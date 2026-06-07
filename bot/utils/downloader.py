"""
DownloadManager — queues and runs torrent download+upload jobs.

Dedup policy
  /download  → no history dedup
  RSS        → full MongoDB dedup

Channel delivery
  /download  → dump_channels from user settings
  RSS        → upload_channels from user settings
  Both respect forward_to_pm toggle.

-replace flag
  Stored per-job; applied to each filename before season/episode extraction.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pyrogram import Client
from pyrogram.types import Message

from config import DOWNLOAD_DIR, MAX_PARALLEL_DOWNLOADS
from bot.utils.torrent import download_torrent
from bot.utils.uploader import upload_file
from bot.utils.episode import extract_season_episode
from bot.utils.arg_parser import apply_replacements
from bot.utils.user_settings import (
    get_dump_channels,
    get_upload_channels,
    forward_to_pm,
)

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}

_PROGRESS_MIN_INTERVAL = 8
_PROGRESS_PCT_STEP     = 10


@dataclass
class DownloadJob:
    id: str
    title: str
    user_id: int
    source: Optional[str]
    torrent_file_id: Optional[str]
    group_chat_id: int
    from_rss: bool = False
    replacements: List[Tuple[str, str]] = field(default_factory=list)
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

    async def enqueue(
        self,
        client: Client,
        message: Message,
        title: str,
        source: Optional[str],
        torrent_file_id: Optional[str],
        from_rss: bool = False,
        replacements: Optional[List[Tuple[str, str]]] = None,
        avoid_keywords: Optional[List[str]] = None,   # accepted but not used here
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
            replacements=replacements or [],
        )
        self._jobs[job_id] = job

        status_msg = await message.reply_text(
            f"🆔 <b>Job:</b> <code>{job_id}</code>\n"
            f"📥 <b>Queued:</b> {title}\n"
            f"⏳ Waiting for a free slot…",
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
            {"id": j.id, "title": j.title, "progress": j.progress, "speed": j.speed}
            for j in self._jobs.values()
            if j.task and not j.task.done()
        ]

    async def _run_job(
        self, job: DownloadJob, status_msg: Message, client: Client
    ) -> None:
        async with self._semaphore:
            job_dir = os.path.join(DOWNLOAD_DIR, job.id)
            os.makedirs(job_dir, exist_ok=True)

            try:
                user_settings: Dict[str, Any] = await client.db.get_settings(job.user_id)

                channels = (
                    get_upload_channels(user_settings) if job.from_rss
                    else get_dump_channels(user_settings)
                )
                do_pm = forward_to_pm(user_settings)

                source = job.source
                if job.torrent_file_id:
                    local_torrent = os.path.join(job_dir, "input.torrent")
                    await client.download_media(job.torrent_file_id, file_name=local_torrent)
                    source = local_torrent

                await _safe_edit(
                    status_msg,
                    f"🆔 <b>Job:</b> <code>{job.id}</code>\n"
                    f"⬇️ <b>Downloading:</b> {job.title}\n"
                    f"⏳ Connecting to peers…",
                )

                start_time    = time.monotonic()
                last_edit_t   = [0.0]
                last_edit_pct = [0.0]

                async def on_progress(pct: float, speed: str, eta: str):
                    job.progress = pct
                    job.speed    = speed
                    job.eta      = eta
                    now     = time.monotonic()
                    elapsed = int(now - start_time)
                    time_ok = (now - last_edit_t[0]) >= _PROGRESS_MIN_INTERVAL
                    pct_ok  = (pct - last_edit_pct[0]) >= _PROGRESS_PCT_STEP
                    if not (time_ok or pct_ok):
                        return
                    if pct == last_edit_pct[0] and not time_ok:
                        return
                    last_edit_t[0]   = now
                    last_edit_pct[0] = pct
                    bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
                    await _safe_edit(
                        status_msg,
                        f"🆔 <b>Job:</b> <code>{job.id}</code>\n"
                        f"⬇️ <b>Downloading:</b> {job.title}\n"
                        f"{bar} {pct:.1f}%\n"
                        f"⚡ {speed}  |  ⏱ ETA: {eta}  |  🕐 {_fmt_elapsed(elapsed)}",
                    )

                all_files = await download_torrent(
                    source=source,
                    dest_dir=job_dir,
                    on_progress=on_progress,
                    cancelled_event=job.cancelled,
                )

                raw_videos = [
                    f for f in all_files
                    if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS
                ]

                def _ep_key(path: str):
                    # Apply replacements before extraction for sort key too
                    adjusted = apply_replacements(os.path.basename(path), job.replacements)
                    _, ep = extract_season_episode(adjusted, user_settings)
                    return ep if ep is not None else 9999

                video_files = sorted(raw_videos, key=_ep_key)

                if not video_files:
                    await _safe_edit(
                        status_msg,
                        f"⚠️ <b>No video files found</b> in torrent <code>{job.id}</code>.",
                    )
                    return

                dest_label = (
                    "channel(s) + PM" if channels and do_pm else
                    "channel(s)"      if channels else
                    "PM"
                )
                await _safe_edit(
                    status_msg,
                    f"✅ <b>Download complete!</b> <code>{job.id}</code>\n"
                    f"📁 {len(video_files)} video file(s)\n"
                    f"📤 Uploading to {dest_label}…",
                )

                db       = client.db
                uploaded = 0
                skipped  = 0

                for vf in video_files:
                    # Apply replacements to the basename for episode key extraction
                    adjusted_name = apply_replacements(os.path.basename(vf), job.replacements)
                    _, episode = extract_season_episode(adjusted_name, user_settings)
                    ep_key = str(episode) if episode is not None else os.path.basename(vf)

                    if job.from_rss and await db.is_duplicate(job.title, ep_key):
                        logger.info("RSS dedup skip: %s ep=%s", job.title, ep_key)
                        skipped += 1
                        continue

                    file_status = await client.send_message(
                        chat_id=status_msg.chat.id,
                        text=f"📤 Uploading: <code>{os.path.basename(vf)}</code>",
                    )
                    await _upload_one(
                        client, vf, job, ep_key, file_status,
                        channels, do_pm, user_settings, db,
                    )
                    uploaded += 1

                if uploaded == 0 and job.from_rss:
                    await _safe_edit(
                        status_msg,
                        f"ℹ️ All {skipped} episode(s) already uploaded. Skipped.",
                    )
                elif skipped:
                    await _safe_edit(
                        status_msg,
                        f"✅ Done — {uploaded} uploaded, {skipped} already existed (skipped).",
                    )

            except asyncio.CancelledError:
                await _safe_edit(status_msg, f"🛑 <b>Cancelled:</b> <code>{job.id}</code>")
            except Exception as exc:
                logger.exception("Job %s failed", job.id)
                await _safe_edit(
                    status_msg,
                    f"❌ <b>Error in job</b> <code>{job.id}</code>:\n<code>{exc}</code>",
                )
            finally:
                shutil.rmtree(job_dir, ignore_errors=True)
                self._jobs.pop(job.id, None)


async def _upload_one(
    client, video_path, job, ep_key, file_status,
    channels, do_pm, user_settings, db,
) -> None:
    try:
        await upload_file(
            client=client,
            video_path=video_path,
            title=job.title,
            requesting_user_id=job.user_id,
            status_message=file_status,
            channels=channels,
            do_forward_pm=do_pm,
            user_settings=user_settings,
            replacements=job.replacements,   # ← passed to uploader/episode
        )
        await db.mark_downloaded(job.title, ep_key, job.user_id)
    except Exception as exc:
        logger.exception("Upload failed for %s", video_path)
        try:
            await file_status.edit_text(f"❌ Upload failed: <code>{exc}</code>")
        except Exception:
            pass


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
