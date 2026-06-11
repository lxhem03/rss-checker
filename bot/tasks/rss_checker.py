"""
RssCheckerTask

Polling: all feeds fetched concurrently via a dedicated ThreadPoolExecutor.

Backlog-dump protection
────────────────────────
If N > 1 unseen entries appear from the same feed in a single check cycle
it almost certainly means:
  a) The feed was just added and the snapshot missed some entries, OR
  b) The provider uploaded several episodes at once (batch release)

In case (a) we don't want to download the whole backlog.
In case (b) — a real batch — the user probably still only wants the latest.

Policy: mark ALL new entries as seen immediately (so they never re-trigger),
but only ENQUEUE the single entry with the highest episode number.
If episode number can't be parsed, take the first entry in feed order
(Nyaa/most providers list newest first).
"""
from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import feedparser

from config import RSS_CHECK_INTERVAL, RSS_FETCH_WORKERS, AUTH_GROUPS
from database import Database
from bot.utils.arg_parser import should_avoid

logger = logging.getLogger(__name__)

# Quick episode-number extractor used only for sorting — no need for the
# full pattern list here, a simple regex on the entry title is enough.
_EP_RE = re.compile(r'[\s\-_](\d{2,3})[\s\-_\[\(]')


def _entry_episode(entry: dict) -> int:
    """Return a sortable episode integer from an entry title, or 0 if unknown."""
    title = entry.get("title", "")
    m = _EP_RE.search(title)
    return int(m.group(1)) if m else 0


class RssCheckerTask:
    def __init__(self, app, db: Database) -> None:
        self._app      = app
        self._db       = db
        self._task: Optional[asyncio.Task] = None
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
            await asyncio.sleep(RSS_CHECK_INTERVAL)

    async def _check_all_feeds(self) -> None:
        feeds = await self._db.get_all_feeds()
        if not feeds:
            return
        logger.debug("Checking %d feed(s) concurrently…", len(feeds))
        results = await asyncio.gather(
            *[self._check_feed(feed) for feed in feeds],
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            logger.warning("%d feed check(s) raised exceptions", len(errors))

    async def _fetch_feed(self, feed_url: str) -> feedparser.FeedParserDict:
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

        # ── Collect unseen entries ────────────────────────────────────────
        new_entries = []
        for entry in parsed.entries:
            guid = entry.get("id") or entry.get("link") or entry.get("title", "")
            if guid and guid not in seen_guids:
                new_entries.append((guid, entry))

        if not new_entries:
            return

        # ── Mark ALL new GUIDs as seen immediately (crash-safe) ───────────
        for guid, _ in new_entries:
            await self._db.mark_guid_seen(feed_id, guid)

        # ── Apply avoid filter ────────────────────────────────────────────
        allowed = []
        for guid, entry in new_entries:
            entry_title = entry.get("title", guid)
            if avoid_keywords and should_avoid(entry_title, avoid_keywords):
                logger.info("Feed '%s': skipping '%s' (avoid match)", title, entry_title)
                continue
            allowed.append((guid, entry))

        if not allowed:
            return

        # ── Backlog-dump protection ───────────────────────────────────────
        # If multiple unseen entries appeared at once, only enqueue the one
        # with the highest episode number. All others are already marked seen
        # above so they will never be re-triggered.
        if len(allowed) > 1:
            # Sort by episode number descending; take the highest
            allowed_sorted = sorted(allowed, key=lambda t: _entry_episode(t[1]), reverse=True)
            chosen_guid, chosen_entry = allowed_sorted[0]
            skipped_titles = [e.get("title", g) for g, e in allowed_sorted[1:]]
            logger.info(
                "Feed '%s': %d new entries detected — enqueueing only latest '%s', "
                "skipping backlog: %s",
                title,
                len(allowed),
                chosen_entry.get("title", chosen_guid),
                skipped_titles,
            )
            to_enqueue = [(chosen_guid, chosen_entry)]
        else:
            to_enqueue = allowed

        # ── Enqueue ───────────────────────────────────────────────────────
        for guid, entry in to_enqueue:
            entry_title = entry.get("title", guid)
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
    """
    Extract torrent URL or magnet from a feedparser entry.
    Prefers magnet links over .torrent URLs to avoid Nyaa 504 errors
    on the direct download endpoint under load.
    """
    magnet   = None
    dot_torrent = None

    # Nyaa RSS extension tag — most reliable magnet source
    nyaa_magnet = entry.get("nyaa_magnetlink", "")
    if nyaa_magnet:
        magnet = nyaa_magnet

    for enc in entry.get("enclosures", []):
        href = enc.get("href") or enc.get("url", "")
        if not href:
            continue
        if href.startswith("magnet:") and not magnet:
            magnet = href
        elif _is_torrent(href) and not dot_torrent:
            dot_torrent = href

    for lnk in entry.get("links", []):
        href = lnk.get("href", "")
        if not href:
            continue
        if href.startswith("magnet:") and not magnet:
            magnet = href
        elif _is_torrent(href) and not dot_torrent:
            dot_torrent = href

    link = entry.get("link", "")
    if link:
        if link.startswith("magnet:") and not magnet:
            magnet = link
        elif _is_torrent(link) and not dot_torrent:
            dot_torrent = link

    # Prefer magnet — bypasses Nyaa's 504-prone .torrent download endpoint
    return magnet or dot_torrent


def _is_torrent(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".torrent")
