"""
Rename → extract metadata → screenshot → upload to user PM.
Progress updates are edited into a status message in AUTH_GROUP.

Caption format (clean, no filesize/duration clutter):
    {Title} - S01E03
    or
    {Title} - E03
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from typing import Optional

from pyrogram import Client
from pyrogram.types import Message

from bot.utils.episode import build_filename, extract_season_episode
from bot.utils.ffmpeg import get_video_metadata, take_screenshot

logger = logging.getLogger(__name__)

_EDIT_INTERVAL = 4  # minimum seconds between progress-message edits


def _make_caption(new_name: str) -> str:
    """
    Return a clean caption: just the bare filename stem (no extension).
    e.g. "Re:Zero - S03E04"
    """
    return os.path.splitext(new_name)[0]


async def upload_file(
    client: Client,
    video_path: str,
    title: str,
    requesting_user_id: int,
    status_message: Message,
) -> None:
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

    # ── 2. Metadata (duration + dimensions) ──────────────────────────────
    duration, width, height = await get_video_metadata(video_path)

    if duration <= 0:
        logger.warning(
            "Duration unknown for '%s' — video will upload without seek bar.", new_name
        )

    # ── 3. Screenshot thumbnail ───────────────────────────────────────────
    thumb_path = await take_screenshot(video_path, work_dir, duration)

    # ── 4. Status update ──────────────────────────────────────────────────
    file_size = os.path.getsize(video_path)
    await _safe_edit(
        status_message,
        f"📤 <b>Uploading:</b> <code>{new_name}</code>\n⬆️ Starting…",
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
        await _safe_edit(
            status_message,
            f"📤 <b>Uploading:</b> <code>{new_name}</code>\n"
            f"{bar} {pct:.1f}%\n"
            f"{_human_size(current)} / {_human_size(total)}",
        )

    # ── 5. Send to user PM ────────────────────────────────────────────────
    # Caption is just the title/season/episode — clean and simple.
    caption = _make_caption(new_name)

    try:
        send_kwargs = dict(
            chat_id=requesting_user_id,
            video=video_path,
            caption=caption,
            # Duration mapped — required for Telegram seek bar
            duration=max(0, int(duration)),
            # Dimensions for correct inline preview aspect ratio
            width=width  if width  > 0 else None,
            height=height if height > 0 else None,
            thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
            supports_streaming=True,
            progress=_progress,
        )
        # Strip explicit None values (Pyrogram dislikes them on optional fields)
        send_kwargs = {k: v for k, v in send_kwargs.items() if v is not None}

        await client.send_video(**send_kwargs)

        await _safe_edit(
            status_message,
            f"✅ <b>Sent to PM:</b> <code>{new_name}</code>",
        )
        logger.info(
            "Uploaded '%s' (duration=%ds, %dx%d) → user %d",
            new_name, duration, width, height, requesting_user_id,
        )

    except Exception as exc:
        logger.exception("Upload failed for %s", video_path)
        await _safe_edit(status_message, f"❌ <b>Upload failed:</b> <code>{exc}</code>")
        raise

    finally:
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
