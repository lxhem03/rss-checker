"""
RssCheckerTask — polls all saved RSS feeds on a fixed interval.

On new entries:
  • Notifies AUTH_GROUP
  • Enqueues via DownloadManager with from_rss=True  (RSS dedup applies)
  • Progress throttling and elapsed timer come from DownloadManager/uploader
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import urlparse

import feedparser

from config import RSS_CHECK_INTERVAL, AUTH_GROUPS
from database import Database

logger = logging.getLogger(__name__)


class RssCheckerTask:
    def __init__(self, app, db: Database) -> None:
        self._app = app
        self._db  = db
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="rss-checker")
        logger.info("RSS checker started (interval=%ds)", RSS_CHECK_INTERVAL)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    # ── Main loop ─────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while True:
            try:
                await self._check_all_feeds()
            except Exception:
                logger.exception("Unhandled error in RSS checker loop")
            await asyncio.sleep(RSS_CHECK_INTERVAL)

    async def _check_all_feeds(self) -> None:
        feeds = await self._db.get_all_feeds()
        if not feeds:
            return
        logger.debug("Checking %d feed(s)…", len(feeds))
        await asyncio.gather(
            *[self._check_feed(feed) for feed in feeds],
            return_exceptions=True,
        )

    async def _check_feed(self, feed: dict) -> None:
        feed_url:   str  = feed["feed_url"]
        title:      str  = feed["title"]
        user_id:    int  = feed["user_id"]
        feed_id          = feed["_id"]
        seen_guids: list = feed.get("seen_guids", [])

        try:
            parsed = await asyncio.get_event_loop().run_in_executor(
                None, feedparser.parse, feed_url
            )
        except Exception as exc:
            logger.warning("Failed to fetch feed %s: %s", feed_url, exc)
            return

        new_entries = []
        for entry in parsed.entries:
            guid = entry.get("id") or entry.get("link") or entry.get("title", "")
            if guid and guid not in seen_guids:
                new_entries.append((guid, entry))

        if not new_entries:
            return

        logger.info("Feed '%s': %d new entry/entries found", title, len(new_entries))

        for guid, entry in new_entries:
            # Mark seen immediately — if we crash mid-download it won't retry
            await self._db.mark_guid_seen(feed_id, guid)

            torrent_url = _extract_torrent_link(entry)
            if not torrent_url:
                logger.warning(
                    "No torrent link in entry '%s', skipping", entry.get("title", guid)
                )
                continue

            await self._enqueue(
                torrent_url=torrent_url,
                title=title,
                user_id=user_id,
                entry_title=entry.get("title", guid),
            )

    async def _enqueue(
        self,
        torrent_url: str,
        title: str,
        user_id: int,
        entry_title: str,
    ) -> None:
        """Send notification then hand off to DownloadManager (from_rss=True)."""
        if not AUTH_GROUPS:
            logger.error("No AUTH_GROUPS configured — cannot send RSS notification")
            return

        group_id = AUTH_GROUPS[0]

        try:
            notify_msg = await self._app.send_message(
                chat_id=group_id,
                text=(
                    f"📡 <b>New RSS entry!</b>\n"
                    f"🏷️ <b>Show:</b> {title}\n"
                    f"📄 <b>Entry:</b> <code>{entry_title}</code>\n"
                    f"⏳ Queueing download…"
                ),
            )
        except Exception as exc:
            logger.error("Could not notify group %s: %s", group_id, exc)
            return

        # Build a minimal message stand-in so DownloadManager can reply to it
        app_ref = self._app

        class _FakeMessage:
            class chat:
                id = group_id
            class from_user:
                id = user_id

            async def reply_text(self_inner, text, **kw):
                try:
                    return await app_ref.send_message(chat_id=group_id, text=text)
                except Exception:
                    return notify_msg

        mgr = self._app.download_manager
        if mgr is None:
            logger.error("DownloadManager not ready yet, skipping RSS entry")
            return

        await mgr.enqueue(
            client=self._app,
            message=_FakeMessage(),
            title=title,
            source=torrent_url,
            torrent_file_id=None,
            from_rss=True,          # ← enables RSS dedup, throttled progress
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_torrent_link(entry: dict) -> Optional[str]:
    """Find a .torrent URL or magnet link from a feedparser entry."""
    # 1. Enclosures (most common in Nyaa)
    for enc in entry.get("enclosures", []):
        href = enc.get("href") or enc.get("url", "")
        if href and (_is_torrent(href) or href.startswith("magnet:")):
            return href

    # 2. links array
    for lnk in entry.get("links", []):
        href = lnk.get("href", "")
        if href and (_is_torrent(href) or href.startswith("magnet:")):
            return href

    # 3. Direct entry link
    link = entry.get("link", "")
    if link and (_is_torrent(link) or link.startswith("magnet:")):
        return link

    # 4. Nyaa RSS extension tag
    magnet = entry.get("nyaa_magnetlink", "")
    if magnet:
        return magnet

    return None


def _is_torrent(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".torrent")
