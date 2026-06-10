"""
Minimal async HTTP health-check server.

Koyeb (and other PaaS platforms) require the container to listen on a TCP
port and respond to HTTP requests or they consider the service unhealthy
and restart it.

This module starts a tiny aiohttp server on $PORT (default 8080) that:
  GET /          → 200 OK  {"status": "ok"}
  GET /health    → 200 OK  {"status": "ok"}
  anything else  → 404

The server runs as a background asyncio task alongside the bot and adds
essentially zero CPU/memory overhead.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_START_TIME: Optional[float] = None


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        # Read just enough to identify the request line — we don't need headers
        data = await asyncio.wait_for(reader.read(512), timeout=5)
        first_line = data.decode("utf-8", errors="replace").split("\n")[0].strip()

        # Parse method and path from "GET /health HTTP/1.1"
        parts = first_line.split()
        path  = parts[1] if len(parts) >= 2 else "/"

        if path in ("/", "/health"):
            body    = json.dumps({"status": "ok"}).encode()
            status  = b"200 OK"
        else:
            body    = json.dumps({"status": "not found"}).encode()
            status  = b"404 Not Found"

        response = (
            b"HTTP/1.1 " + status + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        ) + body

        writer.write(response)
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def start_health_server(port: int) -> asyncio.Server:
    """
    Start the health-check TCP server on *port*.
    Returns the asyncio.Server object (no need to await it further).
    """
    server = await asyncio.start_server(_handle, "0.0.0.0", port)
    logger.info("Health check server listening on port %d", port)
    return server
