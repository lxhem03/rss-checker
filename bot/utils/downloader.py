"""
DownloadManager

Manages a pool of concurrent download+upload jobs.
Each job:
  1. Downloads torrent via libtorrent
  2. For each video file found → uploads to user PM (parallel uploads)
  3. Reports progress to AUTH_GROUP
  4. Marks download as complete in DB for dedup
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from pyrogram import Client
from pyrogram.types import Message

from config import AUTH_GROUPS, DOWNLOAD_DIR, MAX_PARALLEL_DOWNLOADS
from bot.utils.torrent import download_torrent
from bot.utils.uploader import upload_file
from bot.utils.episode import extract_season_episode

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}


@dataclass
class DownloadJob:
    id: str
    title: str
    user_id: int
    source: Optional[str]
    torrent_file_id: Optional[str]
    group_chat_id: int
    progress: float = 0.0
    speed: str = "0 B/s"
    eta: str = "∞"
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    task: Optional[asyncio.Task] = None


class DownloadManager:
    def __init__(self, client: Client):
        self._client = client
        self._jobs: Dict[str, DownloadJob] = {}
        self._semaphore = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)

    # ── Public API ────────────────────────────────────────────────────────

    async def enqueue(
        self,
        client: Client,
        message: Message,
        title: str,
        source: Optional[str],
        torrent_file_id: Optional[str],
    ) -> str:
        job_id = uuid.uuid4().hex[:8]
        group_chat_id = message.chat.id

        # Find first auth group that matches, or fall back to current chat
        auth_group_id = group_chat_id

        job = DownloadJob(
            id=job_id,
            title=title,
            user_id=message.from_user.id,
            source=source,
            torrent_file_id=torrent_file_id,
            group_chat_id=auth_group_id,
        )
        self._jobs[job_id] = job

        status_msg = await message.reply_text(
            f"🆔 <b>Job:</b> <code>{job_id}</code>\n"
            f"📥 <b>Queued:</b> {title}\n"
            f"⏳ Waiting for a free download slot…",
            quote=True,
        )

        job.task = asyncio.create_task(
            self._run_job(job, status_msg, client),
            name=f"job-{job_id}",
        )
        return job_id

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        job.cancelled.set()
        if job.task:
            job.task.cancel()
        return True

    def get_active_jobs(self) -> List[dict]:
        return [
            {
                "id": j.id,
                "title": j.title,
                "progress": j.progress,
                "speed": j.speed,
            }
            for j in self._jobs.values()
            if j.task and not j.task.done()
        ]

    # ── Internal ──────────────────────────────────────────────────────────

    async def _run_job(
        self, job: DownloadJob, status_msg: Message, client: Client
    ) -> None:
        async with self._semaphore:
            job_dir = os.path.join(DOWNLOAD_DIR, job.id)
            os.makedirs(job_dir, exist_ok=True)

            try:
                # ── Resolve torrent source ────────────────────────────────
                source = job.source
                if job.torrent_file_id:
                    local_torrent = os.path.join(job_dir, "input.torrent")
                    await client.download_media(job.torrent_file_id, file_name=local_torrent)
                    source = local_torrent

                await self._safe_edit(
                    status_msg,
                    f"🆔 <b>Job:</b> <code>{job.id}</code>\n"
                    f"⬇️ <b>Downloading:</b> {job.title}\n"
                    f"⏳ Connecting to peers…",
                )

                # ── Progress callback ─────────────────────────────────────
                async def on_progress(pct: float, speed: str, eta: str):
                    job.progress = pct
                    job.speed = speed
                    job.eta = eta
                    bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
                    await self._safe_edit(
                        status_msg,
                        f"🆔 <b>Job:</b> <code>{job.id}</code>\n"
                        f"⬇️ <b>Downloading:</b> {job.title}\n"
                        f"{bar} {pct:.1f}%\n"
                        f"⚡ {speed} | ⏱ ETA: {eta}",
                    )

                # ── Download ──────────────────────────────────────────────
                all_files = await download_torrent(
                    source=source,
                    dest_dir=job_dir,
                    on_progress=on_progress,
                    cancelled_event=job.cancelled,
                )

                # ── Filter video files ────────────────────────────────────
                video_files = sorted([
                    f for f in all_files
                    if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS
                ])

                if not video_files:
                    await self._safe_edit(
                        status_msg,
                        f"⚠️ <b>No video files found</b> in torrent <code>{job.id}</code>.",
                    )
                    return

                await self._safe_edit(
                    status_msg,
                    f"✅ <b>Download complete!</b> <code>{job.id}</code>\n"
                    f"📁 {len(video_files)} video file(s) found.\n"
                    f"📤 Uploading to your PM…",
                )

                db = client.db

                # ── Upload each video (parallel) ──────────────────────────
                upload_tasks = []
                for vf in video_files:
                    _, episode = extract_season_episode(os.path.basename(vf))
                    ep_key = str(episode) if episode else os.path.basename(vf)

                    # Dedup check
                    if await db.is_duplicate(job.title, ep_key):
                        logger.info("Skipping duplicate: %s %s", job.title, ep_key)
                        continue

                    # Per-file status message
                    file_status = await client.send_message(
                        chat_id=status_msg.chat.id,
                        text=f"📤 Uploading: <code>{os.path.basename(vf)}</code>",
                    )
                    upload_tasks.append(
                        self._upload_one(client, vf, job, ep_key, file_status)
                    )

                if upload_tasks:
                    await asyncio.gather(*upload_tasks, return_exceptions=True)

            except asyncio.CancelledError:
                await self._safe_edit(
                    status_msg,
                    f"🛑 <b>Cancelled:</b> <code>{job.id}</code>",
                )
            except Exception as exc:
                logger.exception("Job %s failed", job.id)
                await self._safe_edit(
                    status_msg,
                    f"❌ <b>Error in job</b> <code>{job.id}</code>:\n<code>{exc}</code>",
                )
            finally:
                # Cleanup job directory
                try:
                    shutil.rmtree(job_dir, ignore_errors=True)
                except Exception:
                    pass
                self._jobs.pop(job.id, None)

    async def _upload_one(
        self,
        client: Client,
        video_path: str,
        job: DownloadJob,
        ep_key: str,
        file_status: Message,
    ) -> None:
        db = client.db
        try:
            await upload_file(
                client=client,
                video_path=video_path,
                title=job.title,
                requesting_user_id=job.user_id,
                status_message=file_status,
            )
            await db.mark_downloaded(job.title, ep_key, job.user_id)
        except Exception as exc:
            logger.exception("Upload failed for %s", video_path)
            await self._safe_edit(file_status, f"❌ Upload failed: <code>{exc}</code>")

    @staticmethod
    async def _safe_edit(msg: Message, text: str) -> None:
        try:
            await msg.edit_text(text)
        except Exception:
            pass
