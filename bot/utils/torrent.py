"""
Async torrent downloader using python-libtorrent.

Supports:
  - Magnet links          (preferred — no HTTP fetch needed)
  - Direct .torrent URLs  (retried up to 3× with backoff; 504/5xx → retry)
  - Local .torrent paths  (after Telegram download)

504 / timeout handling
──────────────────────
Nyaa's direct .torrent download endpoint (/download/*.torrent) returns 504
under load. We retry up to MAX_TORRENT_FETCH_RETRIES times with exponential
backoff. If all retries fail the error is re-raised so the caller can decide
what to do (e.g. the /download handler can tell the user to use a magnet).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import AsyncIterator, Callable, List, Optional

import httpx
import libtorrent as lt

from config import DOWNLOAD_DIR, MAX_DOWNLOAD_RATE, MAX_UPLOAD_RATE

logger = logging.getLogger(__name__)

_PROGRESS_INTERVAL      = 3    # seconds between libtorrent status polls
MAX_TORRENT_FETCH_RETRIES = 3  # how many times to retry a failed .torrent URL


def _make_session() -> lt.session:
    ses = lt.session()
    settings: dict = {
        "active_downloads": 10,
        "active_seeds":     4,
    }
    if MAX_DOWNLOAD_RATE:
        settings["download_rate_limit"] = MAX_DOWNLOAD_RATE
    if MAX_UPLOAD_RATE:
        settings["upload_rate_limit"] = MAX_UPLOAD_RATE
    ses.apply_settings(settings)
    return ses


_SESSION: Optional[lt.session] = None


def get_session() -> lt.session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _make_session()
    return _SESSION


async def fetch_torrent_file(url: str, dest_dir: str) -> str:
    """
    Download a .torrent file from *url* with retry + exponential backoff.
    Retries on 5xx responses and connection errors.
    Raises httpx.HTTPStatusError / httpx.RequestError after all retries fail.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_TORRENT_FETCH_RETRIES + 1):
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=30,
                headers={"User-Agent": "Mozilla/5.0 (compatible; RSS-Bot/1.0)"},
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()

            path = os.path.join(dest_dir, "download.torrent")
            with open(path, "wb") as f:
                f.write(resp.content)
            logger.debug("Fetched .torrent from %s (attempt %d)", url, attempt)
            return path

        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            last_exc = exc
            status = getattr(getattr(exc, "response", None), "status_code", "?")
            wait   = 2 ** attempt   # 2s, 4s, 8s
            logger.warning(
                "Torrent fetch attempt %d/%d failed (status=%s): %s — retrying in %ds",
                attempt, MAX_TORRENT_FETCH_RETRIES, status, exc, wait,
            )
            if attempt < MAX_TORRENT_FETCH_RETRIES:
                await asyncio.sleep(wait)

    raise last_exc  # all retries exhausted


async def download_torrent(
    source: str,
    dest_dir: str,
    on_progress: Optional[Callable] = None,
    cancelled_event: Optional[asyncio.Event] = None,
) -> List[str]:
    """
    Download a torrent.

    source      — magnet URI, http(s) .torrent URL, or local file path
    dest_dir    — directory to save files into
    on_progress — async callable(pct, speed_str, eta_str)
    cancelled_event — set to abort

    Returns list of downloaded file paths.
    """
    os.makedirs(dest_dir, exist_ok=True)
    ses = get_session()

    # ── Resolve source to a libtorrent handle ─────────────────────────────
    if source.startswith("magnet:"):
        params       = lt.parse_magnet_uri(source)
        params.save_path = dest_dir
        handle       = ses.add_torrent(params)

    elif source.startswith(("http://", "https://")):
        torrent_path = await fetch_torrent_file(source, dest_dir)
        info         = lt.torrent_info(torrent_path)
        handle       = ses.add_torrent({"ti": info, "save_path": dest_dir})

    else:
        # Local .torrent file path
        info   = lt.torrent_info(source)
        handle = ses.add_torrent({"ti": info, "save_path": dest_dir})

    logger.info("Torrent added, waiting for metadata…")

    # ── Wait for metadata (important for magnets) ─────────────────────────
    while not handle.has_metadata():
        if cancelled_event and cancelled_event.is_set():
            ses.remove_torrent(handle)
            raise asyncio.CancelledError("Download cancelled before metadata")
        await asyncio.sleep(1)

    # ── Download loop ─────────────────────────────────────────────────────
    last_report = 0.0
    while True:
        if cancelled_event and cancelled_event.is_set():
            ses.remove_torrent(handle)
            raise asyncio.CancelledError("Download cancelled by user")

        s = handle.status()

        if time.monotonic() - last_report >= _PROGRESS_INTERVAL:
            last_report = time.monotonic()
            pct         = s.progress * 100
            speed_str   = _human_speed(s.download_rate)
            eta_str     = _calc_eta(s)
            if on_progress:
                try:
                    await on_progress(pct, speed_str, eta_str)
                except Exception:
                    pass

        if s.state in (
            lt.torrent_status.finished,
            lt.torrent_status.seeding,
        ) or s.is_seeding:
            break

        await asyncio.sleep(1)

    # ── Collect output files ──────────────────────────────────────────────
    info  = handle.get_torrent_info()
    files = [
        os.path.join(dest_dir, info.files().file_path(i))
        for i in range(info.files().num_files())
    ]
    ses.remove_torrent(handle)
    return [f for f in files if os.path.exists(f)]


def _human_speed(bps: int) -> str:
    if bps < 1024:
        return f"{bps} B/s"
    if bps < 1024 ** 2:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps / 1024 ** 2:.1f} MB/s"


def _calc_eta(s) -> str:
    if s.download_rate <= 0:
        return "∞"
    remaining = (1 - s.progress) * s.total_wanted
    secs      = int(remaining / s.download_rate)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs // 3600}h {(secs % 3600) // 60}m"
