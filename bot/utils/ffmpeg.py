"""
FFmpeg / ffprobe utilities.

get_video_metadata(path) -> (duration_secs: int, width: int, height: int)
take_screenshot(path, out_dir, duration) -> Optional[str]

Duration-mapping guarantee
──────────────────────────
We try three methods in order:

  1. ffprobe  — fast, reads container header directly.
  2. mediainfo — secondary probe; handles edge-case containers ffprobe
                 occasionally misreads (some older HEVC/AVC streams).
  3. Returns 0 for duration / 0×0 for dimensions if both fail — the
     uploader will still work; Telegram just won't show a seek bar.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# ── Internal runner ───────────────────────────────────────────────────────────

async def _run(*cmd: str) -> Tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")


# ── Primary probe: ffprobe ────────────────────────────────────────────────────

async def _ffprobe_metadata(video_path: str) -> Tuple[int, int, int]:
    """
    Returns (duration_secs, width, height) via ffprobe JSON output.
    All values are 0 on failure.
    """
    code, out, err = await _run(
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        video_path,
    )
    if code != 0:
        logger.debug("ffprobe error for %s: %s", video_path, err)
        return 0, 0, 0

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return 0, 0, 0

    duration = 0
    width = 0
    height = 0

    # Duration: prefer format-level (most accurate), fall back to stream-level
    fmt = data.get("format", {})
    dur_str = fmt.get("duration", "")
    if dur_str:
        try:
            duration = int(float(dur_str))
        except ValueError:
            pass

    # Width / height: first video stream
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            w = stream.get("width", 0)
            h = stream.get("height", 0)
            if w and h:
                width, height = int(w), int(h)

            # Some containers store duration only at the stream level
            if duration <= 0:
                s_dur = stream.get("duration", "")
                if s_dur:
                    try:
                        duration = int(float(s_dur))
                    except ValueError:
                        pass
            break

    return duration, width, height


# ── Fallback probe: mediainfo ─────────────────────────────────────────────────

async def _mediainfo_metadata(video_path: str) -> Tuple[int, int, int]:
    """
    Returns (duration_secs, width, height) via mediainfo.
    All values are 0 on failure.
    """
    code, out, err = await _run(
        "mediainfo",
        "--Output=JSON",
        video_path,
    )
    if code != 0:
        logger.debug("mediainfo error for %s: %s", video_path, err)
        return 0, 0, 0

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return 0, 0, 0

    duration = 0
    width = 0
    height = 0

    tracks = data.get("media", {}).get("track", [])
    for track in tracks:
        t = track.get("@type", "")
        if t == "General" and duration <= 0:
            dur_ms = track.get("Duration", "")
            if dur_ms:
                try:
                    duration = int(float(dur_ms))  # mediainfo returns ms
                    # mediainfo Duration field: some versions return ms, some seconds
                    # Heuristic: if > 100_000 it's likely ms
                    if duration > 100_000:
                        duration = duration // 1000
                except ValueError:
                    pass
        if t == "Video":
            if not width:
                try:
                    width = int(track.get("Width", 0))
                except (ValueError, TypeError):
                    pass
            if not height:
                try:
                    height = int(track.get("Height", 0))
                except (ValueError, TypeError):
                    pass

    return duration, width, height


# ── Public API ────────────────────────────────────────────────────────────────

async def get_video_metadata(video_path: str) -> Tuple[int, int, int]:
    """
    Returns (duration_secs, width, height).

    Tries ffprobe first; falls back to mediainfo if duration is still 0.
    Width/height of 0 means unknown — callers should handle this gracefully.
    """
    duration, width, height = await _ffprobe_metadata(video_path)

    if duration <= 0:
        logger.info(
            "ffprobe returned no duration for '%s', trying mediainfo fallback…",
            os.path.basename(video_path),
        )
        mi_dur, mi_w, mi_h = await _mediainfo_metadata(video_path)
        if mi_dur > 0:
            duration = mi_dur
        if width == 0 and mi_w > 0:
            width = mi_w
        if height == 0 and mi_h > 0:
            height = mi_h

    if duration <= 0:
        logger.warning(
            "Both ffprobe and mediainfo failed to read duration for '%s'. "
            "Video will be uploaded without seek bar.",
            os.path.basename(video_path),
        )

    return duration, width, height


async def get_duration(video_path: str) -> int:
    """Convenience wrapper — returns duration in seconds only."""
    duration, _, _ = await get_video_metadata(video_path)
    return duration


async def take_screenshot(
    video_path: str,
    out_dir: str,
    duration: int = 0,
) -> Optional[str]:
    """
    Capture a single frame as JPEG thumbnail.

    Timestamp is chosen randomly between 10 % and 80 % of the video duration
    so we avoid blank title-card frames at the very start/end.

    If duration is unknown (0) we attempt at a fixed 30-second offset first,
    then at 5 seconds as a last-resort fallback.

    Returns the path to the JPEG file, or None if all attempts failed.
    """
    os.makedirs(out_dir, exist_ok=True)
    thumb_path = os.path.join(out_dir, "thumb.jpg")

    candidates: list[int] = []
    if duration > 10:
        ts = random.randint(max(2, int(duration * 0.10)), int(duration * 0.80))
        candidates = [ts, max(2, ts // 2), 5]
    else:
        candidates = [30, 10, 5, 2]

    for ts in candidates:
        code, _, err = await _run(
            "ffmpeg", "-y",
            "-ss", str(ts),
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            "-vf", "scale=320:-2",   # -2 keeps width divisible by 2 (required by some codecs)
            thumb_path,
        )
        if code == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            logger.debug("Screenshot taken at %ds for '%s'", ts, os.path.basename(video_path))
            return thumb_path

        logger.debug("Screenshot attempt at %ds failed: %s", ts, err[:120])

    logger.warning("All screenshot attempts failed for '%s'", os.path.basename(video_path))
    return None
