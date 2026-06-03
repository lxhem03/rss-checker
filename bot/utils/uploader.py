"""
Handles renaming + uploading a downloaded video file to the requesting user's PM.
Progress updates are sent to AUTH_GROUP.

Duration mapping guarantee
──────────────────────────
Telegram's video player only shows a scrub bar / correct duration when the
`duration` parameter of send_video is a positive integer (seconds).  We:

  1. Run ffprobe to get the exact duration.
  2. If ffprobe fails we fall back to reading the container header via
     `mediainfo` (also bundled in the Dockerfile).
  3. If both fail we leave duration=0 — Telegram will still accept the file,
     it just won't show the seek bar.  A warning is logged.

The `width` and `height` are also extracted and forwarded so Telegram renders
the inline preview at the correct aspect ratio.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from typing import Optional, Tuple

from pyrogram import Client
from pyrogram.types import Message

from bot.utils.episode import build_filename
from bot.utils.ffmpeg import get_video_metadata, take_screenshot

logger = logging.getLogger(__name__)

_EDIT_INTERVAL = 4  # minimum seconds between progress-message edits


async def upload_file(
    client: Client,
    video_path: str,
    title: str,
    requesting_user_id: int,
    status_message: Message,
) -> None:
    """
    Rename → extract metadata → screenshot → upload to user PM.

    *status_message* lives in AUTH_GROUP and is edited with live progress.
    The final file is sent to *requesting_user_id*'s private chat.
    """
    work_dir = os.path.dirname(video_path)

    # ── 1. Rename ─────────────────────────────────────────────────────────
    new_name = build_filename(title, os.path.basename(video_path))
    new_path = os.path.join(work_dir, new_name)
    if os.path.abspath(video_path) != os.path.abspath(new_path):
        shutil.move(video_path, new_path)
    video_path = new_path

    await _safe_edit(
        status_message,
        f"🎬 <b>Processing:</b> <code>{new_name}</code>\n⏳ Extracting video metadata…",
    )

    # ── 2. Extract video metadata (duration + dimensions) ─────────────────
    duration, width, height = await get_video_metadata(video_path)

    if duration <= 0:
        logger.warning(
            "Could not determine duration for '%s'. "
            "Video will upload without seek bar.",
            new_name,
        )

    # ── 3. Screenshot thumbnail ───────────────────────────────────────────
    thumb_path = await take_screenshot(video_path, work_dir, duration)

    # ── 4. Prepare upload ─────────────────────────────────────────────────
    file_size = os.path.getsize(video_path)
    await _safe_edit(
        status_message,
        f"📤 <b>Uploading:</b> <code>{new_name}</code>\n"
        f"📦 Size: {_human_size(file_size)} | "
        f"⏱ Duration: {_fmt_duration(duration)} | "
        f"📐 {width}×{height}\n"
        f"⬆️ Starting upload…",
    )

    last_edit = time.monotonic()

    async def _progress(current: int, total: int) -> None:
        nonlocal last_edit
        now = time.monotonic()
        if now - last_edit < _EDIT_INTERVAL:
            return
        last_edit = now
        pct = current / total * 100 if total else 0
        bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
        speed_bps = current / max(now - last_edit, 1)
        await _safe_edit(
            status_message,
            f"📤 <b>Uploading:</b> <code>{new_name}</code>\n"
            f"{bar} {pct:.1f}%\n"
            f"{_human_size(current)} / {_human_size(total)}",
        )

    # ── 5. Send video to user PM ──────────────────────────────────────────
    try:
        send_kwargs = dict(
            chat_id=requesting_user_id,
            video=video_path,
            caption=(
                f"🎬 <b>{new_name}</b>\n"
                f"⏱ <code>{_fmt_duration(duration)}</code>  |  "
                f"📦 <code>{_human_size(file_size)}</code>"
            ),
            # ── Duration is always set (int seconds ≥ 0) ──────────────────
            # Telegram requires this to be a non-negative integer.
            # When duration > 0 the player shows a proper seek bar.
            duration=max(0, int(duration)),
            # ── Dimensions allow inline playback at correct aspect ratio ───
            width=width if width > 0 else None,
            height=height if height > 0 else None,
            thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
            supports_streaming=True,
            progress=_progress,
        )
        # Strip None values — Pyrogram dislikes explicit None for optional fields
        send_kwargs = {k: v for k, v in send_kwargs.items() if v is not None}

        await client.send_video(**send_kwargs)

        await _safe_edit(
            status_message,
            f"✅ <b>Done!</b> <code>{new_name}</code>\n"
            f"⏱ {_fmt_duration(duration)}  |  📦 {_human_size(file_size)}\n"
            f"📨 Sent to your PM.",
        )
        logger.info("Uploaded '%s' (duration=%ds) to user %d", new_name, duration, requesting_user_id)

    except Exception as exc:
        logger.exception("Upload failed for %s", video_path)
        await _safe_edit(status_message, f"❌ <b>Upload failed:</b> <code>{exc}</code>")
        raise

    finally:
        # Always clean up local files
        _unlink(video_path)
        if thumb_path:
            _unlink(thumb_path)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _safe_edit(msg: Message, text: str) -> None:
    try:
        await msg.edit_text(text)
    except Exception:
        pass


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except Exception:
        pass


def _human_size(b: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _fmt_duration(secs: int) -> str:
    if secs <= 0:
        return "unknown"
    h, r = divmod(int(secs), 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
