"""
Rename → metadata → screenshot → upload.

Delivery logic (per user settings)
────────────────────────────────────
  channels provided   forward_to_pm   result
  ─────────────────   ─────────────   ──────────────────────────────────────
  []                  any             → user PM only (Phase-1 behaviour)
  [ch1, ch2, …]       True            → ch1 (full upload) + ch2…N (forward)
                                        + user PM (forward from ch1)
  [ch1, ch2, …]       False           → ch1 (full upload) + ch2…N (forward)
                                        PM skipped
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from typing import Any, Dict, List, Optional

from pyrogram import Client
from pyrogram.types import Message

from bot.utils.episode import build_filename
from bot.utils.ffmpeg import get_video_metadata, take_screenshot

logger = logging.getLogger(__name__)

_UPLOAD_MIN_INTERVAL = 8
_UPLOAD_PCT_STEP     = 10


def _make_caption(new_name: str) -> str:
    return os.path.splitext(new_name)[0]


async def upload_file(
    client: Client,
    video_path: str,
    title: str,
    requesting_user_id: int,
    status_message: Message,
    # ── Phase-2 additions ──────────────────────────────────────────────────
    channels: Optional[List[int]] = None,        # dump or upload channel IDs
    do_forward_pm: bool = True,                  # forward to user PM too?
    user_settings: Optional[Dict[str, Any]] = None,  # for custom template/patterns
) -> None:
    base_dir  = os.path.dirname(video_path)
    stem      = os.path.splitext(os.path.basename(video_path))[0]
    thumb_dir = os.path.join(base_dir, f"_thumb_{stem}")
    os.makedirs(thumb_dir, exist_ok=True)

    thumb_path = None

    try:
        # ── 1. Rename ─────────────────────────────────────────────────────
        new_name = build_filename(
            title,
            os.path.basename(video_path),
            user_settings=user_settings,
        )
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

        # ── 3. Thumbnail ──────────────────────────────────────────────────
        thumb_path = await take_screenshot(video_path, thumb_dir, duration)

        await _safe_edit(
            status_message,
            f"📤 <b>Uploading:</b> <code>{new_name}</code>\n⬆️ Starting…",
        )

        # ── 4. Throttled progress ─────────────────────────────────────────
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

        # ── 5. Determine delivery targets ─────────────────────────────────
        send_channels = list(channels) if channels else []

        # ── 6. Upload ─────────────────────────────────────────────────────
        caption    = _make_caption(new_name)
        thumb_arg  = thumb_path if thumb_path and os.path.exists(thumb_path) else None

        base_kwargs = dict(
            caption=caption,
            duration=max(0, int(duration)),
            width=width   if width  > 0 else None,
            height=height if height > 0 else None,
            thumb=thumb_arg,
            supports_streaming=True,
        )
        base_kwargs = {k: v for k, v in base_kwargs.items() if v is not None}

        first_msg = None  # we'll reuse its file_id for all subsequent sends

        if send_channels:
            # ── 6a. Full upload to first channel ──────────────────────────
            first_ch = send_channels[0]
            first_msg = await client.send_video(
                chat_id=first_ch,
                video=video_path,
                progress=_progress,
                **base_kwargs,
            )
            logger.info("Uploaded '%s' to channel %d", new_name, first_ch)

            # ── 6b. Forward by file_id to remaining channels ──────────────
            for ch in send_channels[1:]:
                try:
                    await client.send_video(
                        chat_id=ch,
                        video=first_msg.video.file_id,
                        **base_kwargs,
                    )
                    logger.info("Forwarded '%s' to channel %d", new_name, ch)
                except Exception as exc:
                    logger.error("Forward to %d failed: %s", ch, exc)
                    await _safe_edit(
                        status_message,
                        f"⚠️ Failed to forward to <code>{ch}</code>: <code>{exc}</code>",
                    )

            # ── 6c. Optionally also send to user PM ───────────────────────
            if do_forward_pm and first_msg:
                try:
                    await client.send_video(
                        chat_id=requesting_user_id,
                        video=first_msg.video.file_id,
                        **base_kwargs,
                    )
                    logger.info("PM copy of '%s' sent to user %d", new_name, requesting_user_id)
                except Exception as exc:
                    logger.error("PM forward failed for user %d: %s", requesting_user_id, exc)

        else:
            # ── 6d. No channels set — send directly to PM ─────────────────
            await client.send_video(
                chat_id=requesting_user_id,
                video=video_path,
                progress=_progress,
                **base_kwargs,
            )
            logger.info(
                "Uploaded '%s' (dur=%ds %dx%d) → PM user %d",
                new_name, duration, width, height, requesting_user_id,
            )

        await _safe_edit(
            status_message,
            f"✅ <b>Sent:</b> <code>{new_name}</code>"
            + (" (channel + PM)" if send_channels and do_forward_pm else
               " (channel only)" if send_channels else
               " (PM)"),
        )

    except Exception as exc:
        logger.exception("Upload failed for %s", video_path)
        await _safe_edit(status_message, f"❌ <b>Upload failed:</b> <code>{exc}</code>")
        raise

    finally:
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
