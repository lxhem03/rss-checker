"""
DownloadManager

Manages a pool of concurrent download+upload jobs.

Dedup policy
────────────
• /download command  → NO dedup against history. Only blocks if the exact
                       same job_id is already in the active in-memory set
                       (prevents accidental double-tap). Past downloads are
                       always re-downloadable.
• RSS workflow       → Full dedup via MongoDB. Skips any episode already
                       marked as downloaded in the DB.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from pyrogram import Client
from pyrogram.types import Message

from config import DOWNLOAD_DIR, MAX_PARALLEL_DOWNLOADS
from bot.utils.torrent import download_torrent
from bot.utils.uploader import upload_file
from bot.utils.episode import extract_season_episode

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}

# How often (seconds) or what % step triggers a progress edit
_PROGRESS_MIN_INTERVAL = 8      # never edit more often than this
_PROGRESS_PCT_STEP     = 10     # also edit on every N% milestone


@dataclass
class DownloadJob:
    id: str
    title: str
    user_id: int
    source: Optional[str]
    torrent_file_id: Optional[str]
    group_chat_id: int
    from_rss: bool = False          # ← controls dedup behaviour
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
        from_rss: bool = False,
    ) -> str:
        job_id = uuid.uuid4().hex[:8]

        job = DownloadJob(
            id=job_id,
            title=title,
            user_id=message.from_user.id,
            source=source,
            torrent_file_id=torrent_file_id,
            group_chat_id=message.chat.id,
            from_rss=from_rss,
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

                # ── Throttled progress callback ───────────────────────────
                start_time   = time.monotonic()
                last_edit_t  = [0.0]
                last_edit_pct = [0.0]

                async def on_progress(pct: float, speed: str, eta: str):
                    job.progress = pct
                    job.speed    = speed
                    job.eta      = eta

                    now     = time.monotonic()
                    elapsed = int(now - start_time)
                    elapsed_str = _fmt_elapsed(elapsed)

                    time_ok = (now - last_edit_t[0]) >= _PROGRESS_MIN_INTERVAL
                    pct_ok  = (pct - last_edit_pct[0]) >= _PROGRESS_PCT_STEP

                    if not (time_ok or pct_ok):
                        return

                    # Only skip if the text would be identical (pct unchanged)
                    if pct == last_edit_pct[0] and not time_ok:
                        return

                    last_edit_t[0]   = now
                    last_edit_pct[0] = pct

                    bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
                    await self._safe_edit(
                        status_msg,
                        f"🆔 <b>Job:</b> <code>{job.id}</code>\n"
                        f"⬇️ <b>Downloading:</b> {job.title}\n"
                        f"{bar} {pct:.1f}%\n"
                        f"⚡ {speed}  |  ⏱ ETA: {eta}  |  🕐 {elapsed_str}",
                    )

                # ── Download ──────────────────────────────────────────────
                all_files = await download_torrent(
                    source=source,
                    dest_dir=job_dir,
                    on_progress=on_progress,
                    cancelled_event=job.cancelled,
                )

                # Sort by episode number so uploads go E01 → E02 → … in order
                raw_videos = [
                    f for f in all_files
                    if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS
                ]

                def _ep_sort_key(path: str):
                    _, ep = extract_season_episode(os.path.basename(path))
                    return ep if ep is not None else 9999

                video_files = sorted(raw_videos, key=_ep_sort_key)

                if not video_files:
                    await self._safe_edit(
                        status_msg,
                        f"⚠️ <b>No video files found</b> in torrent <code>{job.id}</code>.",
                    )
                    return

                await self._safe_edit(
                    status_msg,
                    f"✅ <b>Download complete!</b> <code>{job.id}</code>\n"
                    f"📁 {len(video_files)} video file(s) — uploading in order…",
                )

                db = client.db
                uploaded = 0
                skipped  = 0

                # Sequential loop — guarantees episode order and isolates
                # each thumbnail in its own subdir (no collisions)
                for vf in video_files:
                    _, episode = extract_season_episode(os.path.basename(vf))
                    ep_key = str(episode) if episode is not None else os.path.basename(vf)

                    # Dedup: RSS only — /download always proceeds
                    if job.from_rss and await db.is_duplicate(job.title, ep_key):
                        logger.info("RSS dedup skip: %s ep=%s", job.title, ep_key)
                        skipped += 1
                        continue

                    file_status = await client.send_message(
                        chat_id=status_msg.chat.id,
                        text=f"📤 Uploading: <code>{os.path.basename(vf)}</code>",
                    )
                    await self._upload_one(client, vf, job, ep_key, file_status)
                    uploaded += 1

                if uploaded == 0 and job.from_rss:
                    await self._safe_edit(
                        status_msg,
                        f"ℹ️ All {skipped} episode(s) already uploaded. Skipped.",
                    )
                elif skipped:
                    await self._safe_edit(
                        status_msg,
                        f"✅ Done — {uploaded} uploaded, {skipped} already existed (skipped).",
                    )

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
                shutil.rmtree(job_dir, ignore_errors=True)
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
            # Always mark as downloaded (both /download and RSS)
            # For RSS this prevents future re-downloads
            # For /download it's just a history record — won't block re-downloads
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


def _fmt_elapsed(secs: int) -> str:
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"
