"""
Rename → extract metadata → screenshot → upload to user PM.

Key design points
─────────────────
• Each call gets its OWN thumbnail subdirectory (thumb_{stem}/) so parallel
  or sequential calls never collide on the same thumb.jpg path.
• FloodWait mitigation: progress edits fire only when ≥8 s have elapsed
  OR ≥10 % progress has been made, AND the text has actually changed.
"""
from __future__ import annotations

import logging
import os
import shutil
import time

from pyrogram import Client
from pyrogram.types import Message

from bot.utils.episode import build_filename
from bot.utils.ffmpeg import get_video_metadata, take_screenshot

logger = logging.getLogger(__name__)

_UPLOAD_MIN_INTERVAL = 8    # seconds between edits
_UPLOAD_PCT_STEP     = 10   # percent milestone that also triggers an edit


def _make_caption(new_name: str) -> str:
    """'Re:Zero - S03E04'  — stem only, no extension, no extra metadata."""
    return os.path.splitext(new_name)[0]


async def upload_file(
    client: Client,
    video_path: str,
    title: str,
    requesting_user_id: int,
    status_message: Message,
) -> None:
    # Each video gets its own working subdir so thumbnails never collide
    base_dir  = os.path.dirname(video_path)
    stem      = os.path.splitext(os.path.basename(video_path))[0]
    thumb_dir = os.path.join(base_dir, f"_thumb_{stem}")
    os.makedirs(thumb_dir, exist_ok=True)

    thumb_path = None

    try:
        # ── 1. Rename ─────────────────────────────────────────────────────
        new_name = build_filename(title, os.path.basename(video_path))
        new_path = os.path.join(base_dir, new_name)
        if os.path.abspath(video_path) != os.path.abspath(new_path):
            shutil.move(video_path, new_path)
        video_path = new_path

        await _safe_edit(
            status_message,
            f"🎬 <b>Processing:</b> <code>{new_name}</code>\n⏳ Extracting metadata…",
        )

        # ── 2. Metadata ───────────────────────────────────────────────────
        duration, width, height = await get_video_metadata(video_path)
        if duration <= 0:
            logger.warning("Duration unknown for '%s' — no seek bar.", new_name)

        # ── 3. Thumbnail — written into thumb_dir, never shared ───────────
        thumb_path = await take_screenshot(video_path, thumb_dir, duration)

        await _safe_edit(
            status_message,
            f"📤 <b>Uploading:</b> <code>{new_name}</code>\n⬆️ Starting…",
        )

        # ── 4. Throttled progress callback ────────────────────────────────
        upload_start   = time.monotonic()
        last_edit_t    = [0.0]
        last_edit_pct  = [-1.0]
        last_edit_text = [""]

        async def _progress(current: int, total: int) -> None:
            now     = time.monotonic()
            pct     = current / total * 100 if total else 0
            elapsed = int(now - upload_start)

            time_ok = (now - last_edit_t[0]) >= _UPLOAD_MIN_INTERVAL
            pct_ok  = (pct - last_edit_pct[0]) >= _UPLOAD_PCT_STEP
            if not (time_ok or pct_ok):
                return

            bar  = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
            text = (
                f"📤 <b>Uploading:</b> <code>{new_name}</code>\n"
                f"{bar} {pct:.1f}%\n"
                f"{_human_size(current)} / {_human_size(total)}"
                f"  |  🕐 {_fmt_elapsed(elapsed)}"
            )
            if text == last_edit_text[0]:
                return

            last_edit_t[0]    = now
            last_edit_pct[0]  = pct
            last_edit_text[0] = text
            await _safe_edit(status_message, text)

        # ── 5. Send to user PM ────────────────────────────────────────────
        send_kwargs = dict(
            chat_id=requesting_user_id,
            video=video_path,
            caption=_make_caption(new_name),
            duration=max(0, int(duration)),
            width=width   if width  > 0 else None,
            height=height if height > 0 else None,
            thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
            supports_streaming=True,
            progress=_progress,
        )
        send_kwargs = {k: v for k, v in send_kwargs.items() if v is not None}

        await client.send_video(**send_kwargs)

        await _safe_edit(
            status_message,
            f"✅ <b>Sent to PM:</b> <code>{new_name}</code>",
        )
        logger.info(
            "Uploaded '%s' (dur=%ds %dx%d) → user %d",
            new_name, duration, width, height, requesting_user_id,
        )

    except Exception as exc:
        logger.exception("Upload failed for %s", video_path)
        await _safe_edit(status_message, f"❌ <b>Upload failed:</b> <code>{exc}</code>")
        raise

    finally:
        # Always clean up this file's private thumb directory
        shutil.rmtree(thumb_dir, ignore_errors=True)
        _unlink(video_path)


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


def _fmt_elapsed(secs: int) -> str:
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"
