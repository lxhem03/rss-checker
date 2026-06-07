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

Peer resolution
───────────────
resolve_all() is called before every send so "peer id invalid" never
surfaces to the user.  Unresolvable channels are reported in the group
status message but don't abort the rest of the upload.
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
from bot.utils.peer import resolve_all

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

        # ── 4. Resolve all peers before touching send_* ───────────────────
        send_channels: List[int] = []
        if channels:
            good, failed = await resolve_all(client, channels)
            send_channels = good
            if failed:
                bad_str = ", ".join(
                    f"<code>{cid}</code> ({reason})" for cid, reason in failed
                )
                await _safe_edit(
                    status_message,
                    f"⚠️ Could not resolve channel(s): {bad_str}\n"
                    f"Continuing with the rest…",
                )
                # Brief pause so user can read the warning
                import asyncio; await asyncio.sleep(2)

        # Also resolve the PM target (user may have never started the bot)
        pm_id: Optional[int] = None
        if do_forward_pm or not send_channels:
            resolved_pm, pm_err = await _resolve_pm(client, requesting_user_id)
            if pm_err:
                logger.warning("Cannot reach PM of user %d: %s", requesting_user_id, pm_err)
            else:
                pm_id = resolved_pm

        # ── 5. Throttled progress ─────────────────────────────────────────
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

        # ── 6. Build common kwargs (no video= yet — added per-call) ───────
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

        # ── 7. Deliver ────────────────────────────────────────────────────
        first_msg = None

        if send_channels:
            # Full upload to first channel
            first_msg = await client.send_video(
                chat_id=send_channels[0],
                video=video_path,
                progress=_progress,
                **base_kw,
            )
            logger.info("Uploaded '%s' to channel %d", new_name, send_channels[0])

            # Forward by file_id to remaining channels
            for ch in send_channels[1:]:
                try:
                    await client.send_video(
                        chat_id=ch,
                        video=first_msg.video.file_id,
                        **base_kw,
                    )
                    logger.info("Forwarded '%s' to channel %d", new_name, ch)
                except Exception as exc:
                    logger.error("Forward to channel %d failed: %s", ch, exc)
                    await _safe_edit(
                        status_message,
                        f"⚠️ Forward to <code>{ch}</code> failed: <code>{exc}</code>",
                    )

            # Optionally forward to PM
            if do_forward_pm and pm_id is not None and first_msg:
                try:
                    await client.send_video(
                        chat_id=pm_id,
                        video=first_msg.video.file_id,
                        **base_kw,
                    )
                    logger.info("PM copy of '%s' → user %d", new_name, requesting_user_id)
                except Exception as exc:
                    logger.error("PM forward failed for user %d: %s", requesting_user_id, exc)

        else:
            # No channels — send directly to PM
            if pm_id is not None:
                await client.send_video(
                    chat_id=pm_id,
                    video=video_path,
                    progress=_progress,
                    **base_kw,
                )
                logger.info(
                    "Uploaded '%s' (dur=%ds %dx%d) → PM user %d",
                    new_name, duration, width, height, requesting_user_id,
                )
            else:
                raise RuntimeError(
                    f"Could not reach PM of user {requesting_user_id} "
                    f"and no channels configured."
                )

        dest_label = (
            "channel(s) + PM" if send_channels and do_forward_pm and pm_id else
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

async def _resolve_pm(client: Client, user_id: int):
    """Thin wrapper — users use positive IDs, no prefix logic needed."""
    from bot.utils.peer import resolve_peer_safe
    return await resolve_peer_safe(client, user_id)


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
