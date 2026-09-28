"""
Async torrent downloader — now backed by aria2c (WZML-X's approach),
not embedded libtorrent.

Why the switch (Heroku)
────────────────────────
The previous implementation used the `libtorrent` Python bindings,
running an in-process torrent session (DHT, alert loop, native C++
extension) directly inside the bot's event loop. On Heroku that's a
known source of trouble:
  - libtorrent's compiled extension has to match the dyno's base image
    exactly, and its DHT/uTP traffic doesn't always play nicely with
    Heroku's containerized networking.
  - A hang or crash inside that native code shares the bot's process —
    it can take the whole dyno down with it.

aria2c — the method WZML-X uses — avoids both problems: it's a single
lightweight, statically-linked binary that runs as its own OS
subprocess, controlled entirely over a local JSON-RPC HTTP API. If it
misbehaves it's isolated from (and independently restartable from) the
bot process, and it has years of track record running on Heroku dynos
specifically (WZML-X itself is a long-running, popular Heroku bot).

Public interface is UNCHANGED from the old implementation:
    await download_torrent(source, dest_dir, on_progress, cancelled_event)
        -> List[str]
so nothing in downloader.py needs to change.

New: call `await Aria2Manager.start()` once at bot startup (see main.py)
before any downloads are attempted, and `await Aria2Manager.stop()` on
shutdown.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
import time
from typing import Any, Callable, Dict, List, Optional

import httpx

from config import DOWNLOAD_DIR, MAX_DOWNLOAD_RATE, MAX_UPLOAD_RATE

logger = logging.getLogger(__name__)

ARIA2_RPC_PORT   = int(os.environ.get("ARIA2_RPC_PORT", "6800"))
ARIA2_RPC_URL    = f"http://127.0.0.1:{ARIA2_RPC_PORT}/jsonrpc"
ARIA2_RPC_SECRET = os.environ.get("ARIA2_RPC_SECRET", "")  # optional shared secret

_PROGRESS_INTERVAL = 3  # seconds between status polls — same cadence as before

# ── aria2c daemon flags ──────────────────────────────────────────────────────
# Adapted from WZML-X's setpkgs.sh (their proven Heroku-safe aria2c launch
# config). Trimmed of the qBittorrent/SABnzbd/CPU-pinning parts, which this
# bot doesn't use — this bot only ever needs aria2 for torrents/magnets.
def _build_flags() -> List[str]:
    flags = [
        "--daemon=true",
        "--enable-rpc=true",
        "--rpc-listen-all=false",
        f"--rpc-listen-port={ARIA2_RPC_PORT}",
        "--rpc-max-request-size=1024M",
        "--max-concurrent-downloads=1000",
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=32M",
        "--optimize-concurrent-downloads=true",
        "--continue=true",
        "--allow-overwrite=true",
        "--content-disposition-default-utf8=true",
        "--user-agent=Wget/1.12",
        "--http-accept-gzip=true",
        "--max-tries=20",
        "--max-file-not-found=0",
        "--check-certificate=false",
        # ── BitTorrent tuning ──────────────────────────────────────────────
        "--bt-enable-lpd=true",
        "--bt-detach-seed-only=true",
        "--bt-remove-unselected-file=true",
        "--bt-max-peers=0",
        "--bt-max-open-files=1000",
        "--bt-request-peer-speed-limit=1M",
        "--seed-ratio=0",
        "--peer-id-prefix=-qB5220-",
        "--peer-agent=qBittorrent/5.2.2",
        # Lets a plain magnet/.torrent-URL added via addUri automatically
        # "follow" into a real torrent download once metadata/the .torrent
        # file itself is fetched — this is what lets us use addUri for
        # BOTH magnets and .torrent URLs, exactly like WZML-X does.
        "--follow-torrent=mem",
        "--reuse-uri=true",
        "--disable-ipv6=false",
        "--connect-timeout=30",
        "--timeout=30",
        "--retry-wait=5",
        "--file-allocation=falloc",
        "--disk-cache=64M",
        "--check-integrity=true",
        "--quiet=true",
        "--summary-interval=0",
    ]
    if MAX_DOWNLOAD_RATE:
        flags.append(f"--max-overall-download-limit={MAX_DOWNLOAD_RATE}")
    if MAX_UPLOAD_RATE:
        flags.append(f"--max-overall-upload-limit={MAX_UPLOAD_RATE}")
    else:
        flags.append("--max-overall-upload-limit=1K")  # we never seed
    if ARIA2_RPC_SECRET:
        flags.append(f"--rpc-secret={ARIA2_RPC_SECRET}")
    return flags


class Aria2Manager:
    """Owns the single aria2c daemon subprocess for this dyno/process."""

    _proc: Optional[asyncio.subprocess.Process] = None
    _lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    async def start(cls) -> None:
        async with cls._lock:
            if cls._proc is not None and cls._proc.returncode is None:
                return  # already running

            binary = shutil.which("aria2c")
            if not binary:
                raise RuntimeError(
                    "aria2c binary not found on PATH. Install the 'aria2' "
                    "system package (see Dockerfile)."
                )

            logger.info("Starting aria2c daemon (rpc port %d)…", ARIA2_RPC_PORT)
            cls._proc = await asyncio.create_subprocess_exec(
                binary, *_build_flags(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

            # aria2 forks into --daemon mode almost instantly, but poll the
            # RPC endpoint briefly rather than assuming a fixed sleep is enough.
            for _ in range(20):
                await asyncio.sleep(0.5)
                try:
                    await _rpc_call("aria2.getVersion", [])
                    logger.info("aria2c RPC is up.")
                    return
                except Exception:
                    continue
            raise RuntimeError("aria2c did not come up on its RPC port in time")

    @classmethod
    async def ensure_running(cls) -> None:
        """Lazily (re)start aria2c if it isn't up — called before every download."""
        if cls._proc is None or cls._proc.returncode is not None:
            if cls._proc is not None:
                logger.warning("aria2c process died — restarting it")
            cls._proc = None
            await cls.start()

    @classmethod
    async def stop(cls) -> None:
        async with cls._lock:
            if cls._proc and cls._proc.returncode is None:
                cls._proc.terminate()
                try:
                    await asyncio.wait_for(cls._proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    cls._proc.kill()
            cls._proc = None


# ── Minimal JSON-RPC client ──────────────────────────────────────────────────

_rpc_id_counter = 0


async def _rpc_call(method: str, params: list) -> Any:
    global _rpc_id_counter
    _rpc_id_counter += 1
    if ARIA2_RPC_SECRET:
        params = [f"token:{ARIA2_RPC_SECRET}"] + list(params)
    payload = {
        "jsonrpc": "2.0",
        "id": str(_rpc_id_counter),
        "method": method,
        "params": params,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(ARIA2_RPC_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
    if "error" in data:
        raise RuntimeError(f"aria2 RPC error on {method}: {data['error']}")
    return data["result"]


async def _remove(gid: str) -> None:
    # Best-effort — the gid may already be gone (completed/never started).
    try:
        await _rpc_call("aria2.forceRemove", [gid])
    except Exception:
        pass


# ── Public API (same contract as the old libtorrent implementation) ────────

async def download_torrent(
    source: str,
    dest_dir: str,
    on_progress: Optional[Callable] = None,
    cancelled_event: Optional[asyncio.Event] = None,
) -> List[str]:
    """
    Download a torrent via aria2c.

    source      — magnet URI, http(s) .torrent URL, or local .torrent file path
    dest_dir    — directory to save files into
    on_progress — async callable(pct, speed_str, eta_str)
    cancelled_event — set to abort

    Returns list of downloaded file paths.
    """
    os.makedirs(dest_dir, exist_ok=True)
    await Aria2Manager.ensure_running()

    options: Dict[str, str] = {"dir": dest_dir}

    if os.path.isfile(source):
        with open(source, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode()
        gid = await _rpc_call("aria2.addTorrent", [b64_data, [], options])
    else:
        # Magnets AND .torrent URLs both go through addUri — the daemon's
        # --follow-torrent=mem flag auto-detects a fetched .torrent file
        # and "follows" it into a real torrent download for us.
        gid = await _rpc_call("aria2.addUri", [[source], options])

    logger.info("aria2: added %s… (gid=%s)", source[:80], gid)

    last_report = 0.0
    status: dict = {}

    while True:
        if cancelled_event and cancelled_event.is_set():
            await _remove(gid)
            raise asyncio.CancelledError("Download cancelled by user")

        try:
            status = await _rpc_call("aria2.tellStatus", [gid])
        except Exception as exc:
            raise RuntimeError(f"aria2 lost track of download: {exc}") from exc

        # A magnet or .torrent-URL add starts as a small metadata-only
        # download; once that finishes aria2 hands off to a NEW gid for
        # the actual torrent data. Follow that chain immediately.
        followed_by = status.get("followedBy")
        if followed_by:
            logger.debug("aria2: gid %s → followed by %s", gid, followed_by[0])
            gid = followed_by[0]
            continue

        state = status.get("status")

        if state == "error":
            err = status.get("errorMessage", "unknown aria2 error")
            await _remove(gid)
            raise RuntimeError(f"aria2 download error: {err}")

        if state == "removed":
            raise asyncio.CancelledError("Download was removed")

        if state == "complete":
            break

        now = time.monotonic()
        if now - last_report >= _PROGRESS_INTERVAL:
            last_report = now
            total = int(status.get("totalLength") or 0)
            done  = int(status.get("completedLength") or 0)
            speed = int(status.get("downloadSpeed") or 0)
            pct   = (done / total * 100) if total else 0.0
            if on_progress:
                try:
                    await on_progress(pct, _human_speed(speed), _calc_eta(done, total, speed))
                except Exception:
                    pass

        await asyncio.sleep(1)

    files = [
        f["path"] for f in status.get("files", [])
        if f.get("path") and f.get("selected", "true") == "true"
    ]
    await _remove(gid)
    return [f for f in files if os.path.exists(f)]


def _human_speed(bps: int) -> str:
    if bps < 1024:
        return f"{bps} B/s"
    if bps < 1024 ** 2:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps / 1024 ** 2:.1f} MB/s"


def _calc_eta(done: int, total: int, speed: int) -> str:
    if speed <= 0 or total <= 0:
        return "∞"
    secs = int((total - done) / speed)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs // 3600}h {(secs % 3600) // 60}m"
