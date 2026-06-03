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
    # S01E01 / S1E1
    re.compile(r"[Ss](?P<season>\d{1,2})[Ee](?P<episode>\d{2,3})"),
    # 1x01
    re.compile(r"(?P<season>\d{1,2})[xX](?P<episode>\d{2,3})"),
    # Season 1 Episode 1
    re.compile(r"[Ss]eason\s*(?P<season>\d{1,2})\s*[Ee]pisode\s*(?P<episode>\d{2,3})"),
    # [12] at end of filename (episode only, no season)
    re.compile(r"\[(?P<episode>\d{2,3})\]"),
    # - 12 - (episode surrounded by dashes/spaces, no season)
    re.compile(r"[\s\-_](?P<episode>\d{2,3})[\s\-_\[]"),
    # E12 / EP12
    re.compile(r"[Ee][Pp]?(?P<episode>\d{2,3})"),
]

# ─────────────────────────────────────────
#  Output filename template
# ─────────────────────────────────────────
FILENAME_TEMPLATE: str = "{title} - S{season:02d}E{episode:02d}.mkv"
FILENAME_TEMPLATE_NO_SEASON: str = "{title} - E{episode:02d}.mkv"
