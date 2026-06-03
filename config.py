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
MONGO_URI: str = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB: str = os.environ.get("MONGO_DB", "rss_bot")

# ─────────────────────────────────────────
#  Download / Upload settings
# ─────────────────────────────────────────
DOWNLOAD_DIR: str = os.environ.get("DOWNLOAD_DIR", "/tmp/downloads")

# How many torrent download workers run in parallel
MAX_PARALLEL_DOWNLOADS: int = int(os.environ.get("MAX_PARALLEL_DOWNLOADS", "3"))

# Pyrogram upload workers (increases upload throughput)
WORKERS: int = int(os.environ.get("WORKERS", "4"))

# libtorrent session upload/download rate limits (bytes/sec, 0 = unlimited)
MAX_DOWNLOAD_RATE: int = int(os.environ.get("MAX_DOWNLOAD_RATE", "0"))
MAX_UPLOAD_RATE: int = int(os.environ.get("MAX_UPLOAD_RATE", "0"))

# ─────────────────────────────────────────
#  RSS Checker
# ─────────────────────────────────────────
# How often to poll feeds, in seconds (between 60 and 300)
RSS_CHECK_INTERVAL: int = max(60, min(300, int(os.environ.get("RSS_CHECK_INTERVAL", "120"))))

# ─────────────────────────────────────────
#  Episode / Season regex patterns
#  Each pattern must have named groups: season (optional) and episode
# ─────────────────────────────────────────
SEASON_EPISODE_PATTERNS: List[re.Pattern] = [

    # S01.E02 / S01-E02 / S01E11 (fixed)
    (re.compile(r'[Ss](\d{1,2})[.\- _]*[Ee](\d{1,3})'),
     ('season', 'episode')),

    # Season 1 Episode 2 (full words)
    (re.compile(r'Season[._\s]+(\d{1,2})[._\s]+Episode[._\s]+(\d{1,3})', re.IGNORECASE),
     ('season', 'episode')),

    # S1 Ep2 (mixed short words)
    (re.compile(r'[Ss](\d{1,2})[._\s]+[Ee]p?[._\s]?(\d{1,3})'),
     ('season', 'episode')),

    # Season-1_Ep-02 type messy formats
    (re.compile(r'Season[._\-\s]*(\d{1,2})[._\-\s]*Ep(?:isode)?[._\-\s]*(\d{1,3})', re.IGNORECASE),
     ('season', 'episode')),

    # New pattern for: S02 - 05, S01 - 12, Demon Slayer S02 - 05 style
    (re.compile(r'[Ss](\d{1,2})[\s._-]*[-–—]?[\s._]*(\d{1,3})', re.IGNORECASE),
     ('season', 'episode')),

    # One Punch man 3 - 12 
    (re.compile(r'[\s._-](\d{1,2})[\s._-]+(\d{1,3})(?=\.[^.]+$)'),
     ('season', 'episode')),

    (re.compile(r'(\d+)(?:st|nd|rd|th)[._\s]*Season[\s._-]*[-–—]?[\s._-]*(\d{1,3})',re.IGNORECASE),
     ('season', 'episode')),

    # Title - 12 (Dual-1080p...) style   ← ADD THIS
    (re.compile(r'[\s-]+\b(\d{1,3})\b\s*(?=\()', re.IGNORECASE),
     ('episode',)),

    # Ep123, Episode 123, ep.123  (require "Ep" or "Episode", not just "e")
    (re.compile(r'(?i)\b(?:ep|episode)[._\s-]*(\d{1,4})\b'),
     ('episode',)),

    # E123 / E 123 / E-123 / E_123  (require word boundary or separator)
    (re.compile(r'(?i)\b[Ee][._\s-]?(\d{1,4})\b'),
     ('episode',)),

    # Season as ordinal (2nd, 3rd) followed by episode number: 2nd_Season_03, 3rd_Season_02
    (re.compile(r'(\d+)(?:st|nd|rd|th)[._\s]*Season[._\s]*(\d{1,3})', re.IGNORECASE),
     ('season', 'episode')),

    # Episode at the very beginning: 003_ or 12. or 001 -
    (re.compile(r'^(\d{2,4})(?=[._\-\s])'),
     ('episode',)),

    # Absolute episode numbers (anime style) - stricter version
    # Only standalone numbers, not part of words like "me123c"
    (re.compile(r'(?<!\w)(\d{2,4})(?!\w)'),
     ('episode',)),
]

# ─────────────────────────────────────────
#  Output filename template
# ─────────────────────────────────────────
FILENAME_TEMPLATE: str = "{title} - S{season:02d}E{episode:02d}.mkv"
FILENAME_TEMPLATE_NO_SEASON: str = "{title} - E{episode:02d}.mkv"
