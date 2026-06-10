"""
RssCheckerTask

Polling strategy
────────────────
All feeds are fetched CONCURRENTLY using a ThreadPoolExecutor sized to
RSS_FETCH_WORKERS (default 10). With 17 feeds this means all 17 HTTP
requests fire at the same time instead of one-by-one, so the full check
cycle completes in roughly the time of the slowest single feed (~2-3 s)
rather than 17 × 2-3 s = 34-51 s.

The interval timer starts AFTER the check completes, so with a 60-second
interval and a 3-second check time you effectively get a new round every
63 seconds — still well under 2 minutes for any number of feeds.

Download concurrency
────────────────────
RSS jobs are enqueued into the DownloadManager which uses
MAX_PARALLEL_DOWNLOADS as its semaphore. To prevent RSS jobs from starving
when multiple shows air at once we raise the default to 10 in config, but
operators can tune it via the env var.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import feedparser

from config import RSS_CHECK_INTERVAL, RSS_FETCH_WORKERS, AUTH_GROUPS
from database import Database
from bot.utils.arg_parser import should_avoid

logger = logging.getLogger(__name__)


class RssCheckerTask:
    def __init__(self, app, db: Database) -> None:
        self._app      = app
        self._db       = db
        self._task: Optional[asyncio.Task] = None
        # Dedicated thread pool for HTTP feed fetches — avoids sharing the
        # default executor with other blocking calls in the process
        self._executor = ThreadPoolExecutor(
            max_workers=RSS_FETCH_WORKERS,
            thread_name_prefix="rss-fetch",
        )

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="rss-checker")
        logger.info(
            "RSS checker started (interval=%ds, fetch_workers=%d)",
            RSS_CHECK_INTERVAL, RSS_FETCH_WORKERS,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        self._executor.shutdown(wait=False)

    # ── Main loop ─────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while True:
            try:
                await self._check_all_feeds()
            except Exception:
                logger.exception("Unhandled error in RSS checker loop")
            # Sleep AFTER the check so the interval is between check-starts,
            # not artificially extended by slow feeds
            await asyncio.sleep(RSS_CHECK_INTERVAL)

    async def _check_all_feeds(self) -> None:
        feeds = await self._db.get_all_feeds()
        if not feeds:
            return

        logger.debug("Checking %d feed(s) concurrently…", len(feeds))

        # Fire all feed checks at the same time; each uses the dedicated executor
        results = await asyncio.gather(
            *[self._check_feed(feed) for feed in feeds],
            return_exceptions=True,
        )

        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            logger.warning("%d feed check(s) raised exceptions", len(errors))

    async def _fetch_feed(self, feed_url: str) -> feedparser.FeedParserDict:
        """Run feedparser.parse in the dedicated thread pool."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, feedparser.parse, feed_url)

    async def _check_feed(self, feed: dict) -> None:
        feed_url:       str  = feed["feed_url"]
        title:          str  = feed["title"]
        user_id:        int  = feed["user_id"]
        feed_id              = feed["_id"]
        seen_guids:     list = feed.get("seen_guids", [])

        raw_replacements = feed.get("replacements", [])
        replacements: List[Tuple[str, str]] = [
            (r[0], r[1]) for r in raw_replacements
            if isinstance(r, (list, tuple)) and len(r) == 2
        ]
        avoid_keywords: List[str] = feed.get("avoid_keywords", [])

        try:
            parsed = await self._fetch_feed(feed_url)
        except Exception as exc:
            logger.warning("Failed to fetch feed '%s': %s", title, exc)
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
            # Mark seen immediately — crash-safe
            await self._db.mark_guid_seen(feed_id, guid)

            entry_title = entry.get("title", guid)

            if avoid_keywords and should_avoid(entry_title, avoid_keywords):
                logger.info("Feed '%s': skipping '%s' (avoid match)", title, entry_title)
                continue

            torrent_url = _extract_torrent_link(entry)
            if not torrent_url:
                logger.warning("No torrent link in '%s', skipping", entry_title)
                continue

            await self._enqueue(
                torrent_url=torrent_url,
                title=title,
                user_id=user_id,
                entry_title=entry_title,
                replacements=replacements,
            )

    async def _enqueue(
        self,
        torrent_url: str,
        title: str,
        user_id: int,
        entry_title: str,
        replacements: List[Tuple[str, str]],
    ) -> None:
        if not AUTH_GROUPS:
            logger.error("No AUTH_GROUPS configured — cannot notify")
            return

        group_id = AUTH_GROUPS[0]

        try:
            notify_msg = await self._app.send_message(
                chat_id=group_id,
                text=(
                    f"📡 <b>New RSS entry!</b>\n"
                    f"🏷️ <b>Show:</b> {title}\n"
                    f"📄 <b>Entry:</b> <code>{entry_title}</code>\n"
                    + ("🔁 <b>Replacements active</b>\n" if replacements else "")
                    + "⏳ Queueing download…"
                ),
            )
        except Exception as exc:
            logger.error("Could not notify group %s: %s", group_id, exc)
            return

        mgr = self._app.download_manager
        if mgr is None:
            logger.error("DownloadManager not ready, skipping '%s'", entry_title)
            return

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

        await mgr.enqueue(
            client=self._app,
            message=_FakeMessage(),
            title=title,
            source=torrent_url,
            torrent_file_id=None,
            from_rss=True,
            replacements=replacements,
            avoid_keywords=[],
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_torrent_link(entry: dict) -> Optional[str]:
    for enc in entry.get("enclosures", []):
        href = enc.get("href") or enc.get("url", "")
        if href and (_is_torrent(href) or href.startswith("magnet:")):
            return href
    for lnk in entry.get("links", []):
        href = lnk.get("href", "")
        if href and (_is_torrent(href) or href.startswith("magnet:")):
            return href
    link = entry.get("link", "")
    if link and (_is_torrent(link) or link.startswith("magnet:")):
        return link
    magnet = entry.get("nyaa_magnetlink", "")
    if magnet:
        return magnet
    return None


def _is_torrent(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".torrent")
