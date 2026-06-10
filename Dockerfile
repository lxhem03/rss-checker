# ─────────────────────────────────────────────────────────────────────────────
#  RSS Torrent Bot — Dockerfile
#  Compatible with: Koyeb, Heroku (container stack), Railway, Render, VPS
#  Base: python:3.11-slim (Debian Bookworm)
#  Extras: ffmpeg, mediainfo, libtorrent-rasterbar (via apt)
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DOWNLOAD_DIR=/tmp/downloads \
    # Default health-check port — Koyeb overrides this with $PORT at runtime
    PORT=8080

# ── System dependencies ───────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        mediainfo \
        python3-libtorrent \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Python packages ───────────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install -r requirements.txt

# ── Application source ────────────────────────────────────────────────────────
COPY . .

# ── Runtime setup ─────────────────────────────────────────────────────────────
RUN mkdir -p /tmp/downloads

# Tell Docker / Koyeb which port the health server listens on
EXPOSE 8080

# Docker-native health check — Koyeb also uses this if present
# Checks every 30s, allows 60s for startup, 3 failures = unhealthy
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health || exit 1

CMD ["python", "main.py"]
