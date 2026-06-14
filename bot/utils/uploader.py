"""
Rename → metadata → screenshot → upload.

Delivery logic (per user settings)
────────────────────────────────────
  channels set  forward_to_pm   result
  ────────────  ─────────────   ──────────────────────────────────────────
  []            any             → PM only (Phase-1 behaviour)
  [ch1,…]       True            → full upload to ch1, forward to ch2…N,
                                  then forward to PM
  [ch1,…]       False           → full upload to ch1, forward to ch2…N,
                                  PM skipped
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

from pyrogram import Client
from pyrogram.types import Message

from bot.utils.episode import build_filename
from bot.utils.arg_parser import apply_replacements
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
    channels: Optional[List[int]] = None,
    do_forward_pm: bool = True,
    user_settings: Optional[Dict[str, Any]] = None,
    replacements: Optional[List[Tuple[str, str]]] = None,
    no_season: bool = False,
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
            replacements=replacements or [],
            no_season=no_season,
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

        # ── 4. Throttled progress callback ────────────────────────────────
        upload_start   = time.monotonic()
        last_edit_t    = [0.0]
        last_edit_pct  = [-1.0]   # highest pct seen — never goes backwards
        last_edit_text = [""]
        peak_current   = [0]      # highest bytes seen — never goes backwards

        async def _progress(current: int, total: int) -> None:
            now = time.monotonic()

            # Pyrogram resets current→0 when it retries a failed chunk.
            # Clamp to the highest value we have seen so the bar never
            # goes backwards and the user doesn't see confusing resets.
            if current > peak_current[0]:
                peak_current[0] = current
            display_current = peak_current[0]

            pct     = display_current / total * 100 if total else 0
            elapsed = int(now - upload_start)
            time_ok = (now - last_edit_t[0]) >= _UPLOAD_MIN_INTERVAL
            pct_ok  = (pct - last_edit_pct[0]) >= _UPLOAD_PCT_STEP
            if not (time_ok or pct_ok):
                return
            bar  = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
            text = (
                f"📤 <b>Uploading:</b> <code>{new_name}</code>\n"
                f"{bar} {pct:.1f}%\n"
                f"{_human_size(display_current)} / {_human_size(total)}"
                f"  |  🕐 {_fmt_elapsed(elapsed)}"
            )
            if text == last_edit_text[0]:
                return
            last_edit_t[0]    = now
            last_edit_pct[0]  = pct
            last_edit_text[0] = text
            await _safe_edit(status_message, text)

        # ── 5. Build common send kwargs ───────────────────────────────────
        caption   = _make_caption(new_name)
        thumb_arg = thumb_path if thumb_path and os.path.exists(thumb_path) else None
        base_kw   = dict(
            caption=caption,
            duration=max(0, int(duration)),
            width=width   if width  > 0 else None,
            height=height if height > 0 else None,
            thumb=thumb_arg,
            supports_streaming=True,
        )
        base_kw = {k: v for k, v in base_kw.items() if v is not None}

        # base_kw WITHOUT progress — used for all file_id forwards so
        # Pyrogram does NOT trigger the progress callback a second time.
        # file_id sends are instant Telegram-side copies, not real uploads.
        forward_kw = {k: v for k, v in base_kw.items()}

        send_channels: List[int] = list(channels) if channels else []

        # ── 6. Deliver ────────────────────────────────────────────────────
        # Strategy:
        #   • ONE real upload (file bytes → Telegram) — always to the first
        #     destination, with the progress bar attached.
        #   • ALL other destinations receive an instant file_id forward —
        #     no re-upload, no second progress bar, takes < 1 second each.
        #
        # Delivery order:
        #   channels set  + PM on  → upload to ch[0], forward ch[1..N], forward PM
        #   channels set  + PM off → upload to ch[0], forward ch[1..N]
        #   no channels   + PM on  → upload directly to PM
        #   no channels   + PM off → upload directly to PM (PM is always fallback)

        first_msg  = None
        first_dest = None   # where the real upload went — for logging

        if send_channels:
            first_dest = send_channels[0]
            try:
                # ── Real upload (one time only) ───────────────────────────
                first_msg = await client.send_video(
                    chat_id=first_dest,
                    video=video_path,       # actual file bytes
                    progress=_progress,     # progress bar shown here only
                    **base_kw,
                )
                logger.info("Uploaded '%s' to channel %d", new_name, first_dest)
            except Exception as exc:
                logger.error("Upload to channel %d failed: %s", first_dest, exc)
                await _safe_edit(
                    status_message,
                    f"⚠️ Channel <code>{first_dest}</code> upload failed: "
                    f"<code>{exc}</code>\n📨 Sending to PM instead…",
                )
                # Channel failed — upload directly to PM as fallback.
                # Reset peak_current so the progress bar starts fresh.
                peak_current[0]  = 0
                last_edit_pct[0] = -1.0
                first_msg = await client.send_video(
                    chat_id=requesting_user_id,
                    video=video_path,
                    progress=_progress,
                    **base_kw,
                )
                first_dest = requesting_user_id
                logger.info("Fallback upload to PM for user %d", requesting_user_id)

            # ── Forward by file_id to remaining channels ──────────────────
            # Instant Telegram-side copy — no re-upload, no progress bar.
            for ch in send_channels[1:]:
                try:
                    await client.send_video(
                        chat_id=ch,
                        video=first_msg.video.file_id,
                        **forward_kw,       # no progress= here
                    )
                    logger.info("Forwarded '%s' → channel %d", new_name, ch)
                except Exception as exc:
                    logger.error("Forward to %d failed: %s", ch, exc)
                    await _safe_edit(
                        status_message,
                        f"⚠️ Forward to <code>{ch}</code> failed: <code>{exc}</code>",
                    )

            # ── Forward to PM (instant, no re-upload) ─────────────────────
            if do_forward_pm and first_dest != requesting_user_id:
                try:
                    await client.send_video(
                        chat_id=requesting_user_id,
                        video=first_msg.video.file_id,
                        **forward_kw,       # no progress= here
                    )
                    logger.info("Forwarded '%s' → PM user %d", new_name, requesting_user_id)
                except Exception as exc:
                    logger.error("PM forward failed for user %d: %s", requesting_user_id, exc)

        else:
            # No channels configured — single upload directly to PM
            first_dest = requesting_user_id
            first_msg  = await client.send_video(
                chat_id=requesting_user_id,
                video=video_path,
                progress=_progress,     # progress bar shown here only
                **base_kw,
            )
            logger.info(
                "Uploaded '%s' (dur=%ds %dx%d) → PM user %d",
                new_name, duration, width, height, requesting_user_id,
            )

        dest_label = (
            "channel(s) + PM" if send_channels and do_forward_pm else
            "channel(s)"      if send_channels else
            "PM"
        )
        await _safe_edit(
            status_message,
            f"✅ <b>Sent:</b> <code>{new_name}</code> → {dest_label}",
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
