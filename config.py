import os
from typing import List
import re

# ─────────────────────────────────────────
#  Telegram credentials
# ─────────────────────────────────────────
API_ID: int = int(os.environ.get("API_ID", 0))
API_HASH: str = os.environ.get("API_HASH", "")
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")

# ─────────────────────────────────────────
#  Access control
# ─────────────────────────────────────────
# Comma-separated user IDs in env, e.g. "123456,789012"
AUTH_USERS: List[int] = [
    int(uid.strip())
    for uid in os.environ.get("AUTH_USERS", "").split(",")
    if uid.strip().isdigit()
]

# Comma-separated group/channel IDs (negative for groups), e.g. "-1001234567890"
AUTH_GROUPS: List[int] = [
    int(gid.strip())
    for gid in os.environ.get("AUTH_GROUPS", "").split(",")
    if gid.strip().lstrip("-").isdigit()
]

# ─────────────────────────────────────────
#  MongoDB
# ─────────────────────────────────────────
MONGO_URI: str = os.environ.get("MONGO_URI", "")
MONGO_DB: str = os.environ.get("MONGO_DB", "rss_bot")

# ─────────────────────────────────────────
#  Download / Upload settings
# ─────────────────────────────────────────
DOWNLOAD_DIR: str = os.environ.get("DOWNLOAD_DIR", "/tmp/downloads")

# How many torrent download workers run in parallel
MAX_PARALLEL_DOWNLOADS: int = int(os.environ.get("MAX_PARALLEL_DOWNLOADS", "3"))

# Pyrogram upload workers (increases upload throughput)
WORKERS: int = int(os.environ.get("WORKERS", "4"))

# Parallel chunk transmissions per file upload.
# 1  = default Pyrogram (slow, sequential chunks)
# 100 = WZML-X setting, saturates the uplink for ~10 MB/s on good servers
MAX_CONCURRENT_TRANSMISSIONS: int = int(
    os.environ.get("MAX_CONCURRENT_TRANSMISSIONS", "100")
)

# libtorrent session upload/download rate limits (bytes/sec, 0 = unlimited)
MAX_DOWNLOAD_RATE: int = int(os.environ.get("MAX_DOWNLOAD_RATE", "0"))
MAX_UPLOAD_RATE: int = int(os.environ.get("MAX_UPLOAD_RATE", "0"))

# ─────────────────────────────────────────
#  RSS Checker
# ─────────────────────────────────────────
# How often to poll feeds, in seconds (between 60 and 300)
RSS_CHECK_INTERVAL: int = max(60, min(300, int(os.environ.get("RSS_CHECK_INTERVAL", "120"))))

# ─────────────────────────────────────────
#  Season / Episode patterns
#
#  Format: (compiled_re, group_name_tuple)
#
#  group_name_tuple entries must be 'season' and/or 'episode' in the same
#  order as the capture groups in the pattern.
#
#  Patterns are tried in ORDER — put the most specific ones first.
# ─────────────────────────────────────────
SEASON_EPISODE_PATTERNS = [

    # ── P1: Standard SxxExx ──────────────────────────────────────────────
    # S04E05  S01E01v2  S01E04-Title  (v-suffix after episode is ignored)
    (
        re.compile(r'[Ss](\d{1,2})\s*[Ee](\d{1,3})'),
        ('season', 'episode'),
    ),

    # ── P2: "N(st|nd|rd|th) Season … - EP" ──────────────────────────────
    # Season 2 - 11
    # 5th Season - 07
    # 4th Season: 2-nensei-hen 1 Gakki - 12
    # Captures: last number before the word "Season", episode after the dash
    (
        re.compile(
            r'(\d+)(?:st|nd|rd|th)?\s+Season\b.*?[-\u2013\u2014]\s*(\d{1,3})'
            r'(?=\s*[\[\(]|\s*$)',
            re.IGNORECASE | re.DOTALL,
        ),
        ('season', 'episode'),
    ),

    # ── P3: "Title N - EP" (bare digit season before dash+episode) ───────
    # Jihanki 3 - 09
    # Rule: single digit 1–9 as a standalone word, then " - " then 2-3 digit ep
    # Negative lookbehind ensures it's not part of a longer number (e.g. 1080)
    (
        re.compile(r'(?<!\d)\b([1-9])\s+-\s+(\d{2,3})(?=\s*[\[\(]|\s*$)'),
        ('season', 'episode'),
    ),

    # ── P4: " - EP" episode-only (no season) ─────────────────────────────
    # - 09 (1080p)    - 09 [1080p    (SubsPlease / Erai-raws no-season style)
    # Negative lookbehind on \d prevents matching the season leg of P3
    (
        re.compile(r'(?<!\d)\s+-\s+(\d{2,3})\s*[\[\(]'),
        ('episode',),
    ),

    # ── P5: Ep / Episode keyword ─────────────────────────────────────────
    # Ep12  Episode 12  ep.12
    (
        re.compile(r'(?i)\b(?:ep|episode)[._\s-]*(\d{1,4})\b'),
        ('episode',),
    ),
]

# ─────────────────────────────────────────
#  Output filename template
# ─────────────────────────────────────────
FILENAME_TEMPLATE: str = "{title} - S{season:02d}E{episode:02d}.mkv"
FILENAME_TEMPLATE_NO_SEASON: str = "{title} - E{episode:02d}.mkv"
