# ─────────────────────────────────────────────────────────────────────────────
#  RSS Torrent Bot — Dockerfile
#  Base: python:3.11-slim (Debian Bookworm)
#  Extras: ffmpeg, mediainfo, libtorrent-rasterbar (via apt)
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DOWNLOAD_DIR=/tmp/downloads

# ── System dependencies ───────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        mediainfo \
        # libtorrent runtime library
        python3-libtorrent \
        # build tools (needed for some pip wheels)
        build-essential \
        # networking
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Python packages ───────────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install -r requirements.txt

# ── Application source ────────────────────────────────────────────────────────
COPY . .

# ── Runtime ───────────────────────────────────────────────────────────────────
RUN mkdir -p /tmp/downloads

CMD ["python", "main.py"]
