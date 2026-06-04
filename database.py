"""
Async MongoDB wrapper using Motor.

Collections
───────────
  rss_feeds    — saved RSS feeds per user
  downloads    — completed download history (dedup store)
  user_settings — per-user Phase-2 settings
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorClient

from config import MONGO_URI, MONGO_DB

logger = logging.getLogger(__name__)

# ── Default settings doc ──────────────────────────────────────────────────────
_DEFAULT_SETTINGS: Dict[str, Any] = {
    "dump_channels":    [],          # list[int]  — for /download
    "upload_channels":  [],          # list[int]  — for /rssfeed
    "rename_template":  None,        # str | None — e.g. "{title} - S{season:02d}E{episode:02d}"
    "custom_patterns":  [],          # list[str]  — raw regex strings added via /settings
    "forward_to_pm":    True,        # bool       — also send to user PM when channel is set
}


class Database:
    def __init__(self) -> None:
        self._client: Optional[AsyncIOMotorClient] = None
        self.feeds     = None
        self.downloads = None
        self.settings  = None

    async def connect(self) -> None:
        self._client = AsyncIOMotorClient(
            MONGO_URI,
            serverSelectionTimeoutMS=10_000,
        )
        await self._client.admin.command("ping")
        logger.info("MongoDB Atlas ping OK.")

        db             = self._client[MONGO_DB]
        self.feeds     = db["rss_feeds"]
        self.downloads = db["downloads"]
        self.settings  = db["user_settings"]

        await self.feeds.create_index("feed_url")
        await self.feeds.create_index("user_id")
        await self.downloads.create_index(
            [("title", 1), ("episode_key", 1)], unique=True
        )
        await self.settings.create_index("user_id", unique=True)
        logger.info("MongoDB connected and indexes ensured.")

    # ── RSS Feeds ─────────────────────────────────────────────────────────

    async def add_feed(self, user_id: int, feed_url: str, title: str) -> Dict[str, Any]:
        existing = await self.feeds.find_one({"feed_url": feed_url, "user_id": user_id})
        if existing:
            return existing
        doc = {
            "feed_url":   feed_url,
            "title":      title,
            "user_id":    user_id,
            "added_at":   datetime.now(timezone.utc),
            "seen_guids": [],
        }
        await self.feeds.insert_one(doc)
        return doc

    async def get_all_feeds(self) -> List[Dict[str, Any]]:
        return await self.feeds.find({}).to_list(length=None)

    async def get_user_feeds(self, user_id: int) -> List[Dict[str, Any]]:
        return await self.feeds.find({"user_id": user_id}).to_list(length=None)

    async def mark_guid_seen(self, feed_id: Any, guid: str) -> None:
        await self.feeds.update_one(
            {"_id": feed_id},
            {"$addToSet": {"seen_guids": guid}},
        )

    async def remove_feed(self, user_id: int, feed_url: str) -> bool:
        result = await self.feeds.delete_one({"feed_url": feed_url, "user_id": user_id})
        return result.deleted_count > 0

    # ── Download dedup ────────────────────────────────────────────────────

    async def is_duplicate(self, title: str, episode_key: str) -> bool:
        return bool(await self.downloads.find_one(
            {"title": title, "episode_key": episode_key}
        ))

    async def mark_downloaded(self, title: str, episode_key: str, user_id: int) -> None:
        try:
            await self.downloads.insert_one({
                "title":       title,
                "episode_key": episode_key,
                "user_id":     user_id,
                "finished_at": datetime.now(timezone.utc),
            })
        except Exception:
            pass

    # ── User Settings ─────────────────────────────────────────────────────

    async def get_settings(self, user_id: int) -> Dict[str, Any]:
        """Return user settings, creating defaults if first access."""
        doc = await self.settings.find_one({"user_id": user_id})
        if doc is None:
            doc = {"user_id": user_id, **_DEFAULT_SETTINGS}
            await self.settings.insert_one(doc)
        else:
            # Backfill any new keys added after initial creation
            missing = {k: v for k, v in _DEFAULT_SETTINGS.items() if k not in doc}
            if missing:
                await self.settings.update_one({"user_id": user_id}, {"$set": missing})
                doc.update(missing)
        return doc

    async def update_setting(self, user_id: int, key: str, value: Any) -> None:
        await self.settings.update_one(
            {"user_id": user_id},
            {"$set": {key: value}},
            upsert=True,
        )

    async def add_custom_pattern(self, user_id: int, pattern_str: str) -> None:
        await self.settings.update_one(
            {"user_id": user_id},
            {"$addToSet": {"custom_patterns": pattern_str}},
            upsert=True,
        )

    async def remove_custom_pattern(self, user_id: int, pattern_str: str) -> None:
        await self.settings.update_one(
            {"user_id": user_id},
            {"$pull": {"custom_patterns": pattern_str}},
        )
