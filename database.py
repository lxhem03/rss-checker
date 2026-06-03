"""
Async MongoDB wrapper using Motor.
Collections:
  - rss_feeds   : { feed_url, title, user_id, added_at, seen_guids: [str] }
  - downloads   : { title, episode_key, user_id, finished_at }   (dedup store)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorClient

from config import MONGO_URI, MONGO_DB

logger = logging.getLogger(__name__)


class Database:
    def __init__(self) -> None:
        self._client: Optional[AsyncIOMotorClient] = None
        self.feeds = None
        self.downloads = None

    async def connect(self) -> None:
        # tlsAllowInvalidCertificates=False is the safe default for Atlas.
        # serverSelectionTimeoutMS=10000 gives a clear error fast instead of
        # hanging for 30 s if credentials / URI are wrong.
        self._client = AsyncIOMotorClient(
            MONGO_URI,
            serverSelectionTimeoutMS=10_000,
        )

        # Force an actual round-trip so we fail loudly here (not later)
        # rather than crashing silently mid-operation.
        await self._client.admin.command("ping")
        logger.info("MongoDB Atlas ping OK.")

        db = self._client[MONGO_DB]
        self.feeds     = db["rss_feeds"]
        self.downloads = db["downloads"]

        # Indexes — safe to call even if they already exist
        await self.feeds.create_index("feed_url")
        await self.feeds.create_index("user_id")
        await self.downloads.create_index(
            [("title", 1), ("episode_key", 1)], unique=True
        )
        logger.info("MongoDB connected and indexes ensured.")

    # ── RSS Feeds ──────────────────────────────────────────────────────────

    async def add_feed(self, user_id: int, feed_url: str, title: str) -> Dict[str, Any]:
        """Insert a new feed or return the existing one."""
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

    # ── Download dedup ─────────────────────────────────────────────────────

    async def is_duplicate(self, title: str, episode_key: str) -> bool:
        doc = await self.downloads.find_one({"title": title, "episode_key": episode_key})
        return doc is not None

    async def mark_downloaded(self, title: str, episode_key: str, user_id: int) -> None:
        try:
            await self.downloads.insert_one(
                {
                    "title":       title,
                    "episode_key": episode_key,
                    "user_id":     user_id,
                    "finished_at": datetime.now(timezone.utc),
                }
            )
        except Exception:
            pass  # duplicate key — already stored, that's fine
