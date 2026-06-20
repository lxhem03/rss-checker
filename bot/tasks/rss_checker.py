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
from typing import Any, Dict, List, Optional, Tuple
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
        # In-memory tracking of GUIDs that currently have a download/upload
        # job running. This is intentionally NOT persisted to MongoDB —
        # if the bot restarts, this dict is empty again, so any entry that
        # was mid-download gets naturally re-discovered as "new" on the
        # next poll and retried. The corresponding GUID is only written
        # to MongoDB's seen_guids (permanent) once the job actually
        # finishes successfully — see _make_on_complete() below.
        self._pending: Dict[Any, set] = {}

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
        pending = self._pending.setdefault(feed_id, set())

        raw_replacements = feed.get("replacements", [])
        replacements: List[Tuple[str, str]] = [
            (r[0], r[1]) for r in raw_replacements
            if isinstance(r, (list, tuple)) and len(r) == 2
        ]
        avoid_keywords: List[str] = feed.get("avoid_keywords", [])
        no_season:      bool       = bool(feed.get("no_season", False))

        try:
            parsed = await self._fetch_feed(feed_url)
        except Exception as exc:
            logger.warning("Failed to fetch feed '%s': %s", title, exc)
            return

        # ── Collect unseen entries ────────────────────────────────────────
        # Exclude both permanently-seen (MongoDB) AND currently in-flight
        # (in-memory "pending") GUIDs. The pending check prevents the same
        # entry from being queued twice while its download/upload is still
        # running across consecutive poll cycles.
        new_entries = []
        for entry in parsed.entries:
            guid = entry.get("id") or entry.get("link") or entry.get("title", "")
            if guid and guid not in seen_guids and guid not in pending:
                new_entries.append((guid, entry))

        if not new_entries:
            return

        # ── Apply avoid filter ────────────────────────────────────────────
        # These are permanently and intentionally skipped, so we mark them
        # seen immediately — there's nothing to download, ever.
        allowed = []
        for guid, entry in new_entries:
            entry_title = entry.get("title", guid)
            if avoid_keywords and should_avoid(entry_title, avoid_keywords):
                logger.info("Feed '%s': skipping '%s' (avoid match)", title, entry_title)
                await self._db.mark_guid_seen(feed_id, guid)
                continue
            allowed.append((guid, entry))

        if not allowed:
            return

        # ── Backlog-dump protection ───────────────────────────────────────
        # If multiple unseen entries appeared at once, only the one with
        # the highest episode number is downloaded. The rest are discarded
        # backlog and marked seen immediately — they were never going to
        # be downloaded regardless of restarts.
        if len(allowed) > 1:
            allowed_sorted = sorted(allowed, key=lambda t: _entry_episode(t[1]), reverse=True)
            chosen_guid, chosen_entry = allowed_sorted[0]
            backlog = allowed_sorted[1:]
            skipped_titles = [e.get("title", g) for g, e in backlog]
            logger.info(
                "Feed '%s': %d new entries detected — enqueueing only latest '%s', "
                "skipping backlog: %s",
                title,
                len(allowed),
                chosen_entry.get("title", chosen_guid),
                skipped_titles,
            )
            for backlog_guid, _ in backlog:
                await self._db.mark_guid_seen(feed_id, backlog_guid)
            to_enqueue = [(chosen_guid, chosen_entry)]
        else:
            to_enqueue = allowed

        # ── Enqueue the chosen entry ───────────────────────────────────────
        # IMPORTANT: this entry's GUID is NOT marked seen here. It only
        # gets added to MongoDB's seen_guids once the download+upload job
        # actually completes (see _make_on_complete). Until then it lives
        # only in the in-memory `pending` set, which is wiped on restart —
        # so a bot crash/redeploy mid-job means this entry gets correctly
        # re-discovered and retried on the next poll, instead of being
        # silently lost forever.
        for guid, entry in to_enqueue:
            entry_title = entry.get("title", guid)
            torrent_url = _extract_torrent_link(entry)

            if not torrent_url:
                logger.warning("No torrent link in '%s', marking seen (nothing to retry)", entry_title)
                await self._db.mark_guid_seen(feed_id, guid)
                continue

            pending.add(guid)

            await self._enqueue(
                torrent_url=torrent_url,
                title=title,
                user_id=user_id,
                entry_title=entry_title,
                replacements=replacements,
                no_season=no_season,
                feed_id=feed_id,
                guid=guid,
            )

    def _make_on_complete(self, feed_id: Any, guid: str):
        """
        Build the on_complete callback passed to DownloadManager.enqueue().

        mark_done=True  → job succeeded / had nothing to do / was cancelled
                           by a user → permanently mark this GUID seen.
        mark_done=False → job failed with a real error (or the bot was
                           killed mid-job, in which case this callback
                           never even runs) → leave it unmarked so the
                           next poll cycle re-discovers and retries it.
        """
        async def _on_complete(mark_done: bool) -> None:
            self._pending.get(feed_id, set()).discard(guid)
            if mark_done:
                await self._db.mark_guid_seen(feed_id, guid)
            else:
                logger.info(
                    "Job for guid=%s failed — will retry on next RSS poll", guid
                )
        return _on_complete

    async def _enqueue(
        self,
        torrent_url: str,
        title: str,
        user_id: int,
        entry_title: str,
        replacements: List[Tuple[str, str]],
        no_season: bool,
        feed_id: Any,
        guid: str,
    ) -> None:
        if not AUTH_GROUPS:
            logger.error("No AUTH_GROUPS configured — cannot notify")
            self._pending.get(feed_id, set()).discard(guid)
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
            # Notification failed before any job started — nothing is
            # actually pending, so leave the GUID unmarked in MongoDB too,
            # it will be picked up again on the next poll.
            self._pending.get(feed_id, set()).discard(guid)
            return

        mgr = self._app.download_manager
        if mgr is None:
            logger.error("DownloadManager not ready, skipping '%s'", entry_title)
            self._pending.get(feed_id, set()).discard(guid)
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
            no_season=no_season,
            on_complete=self._make_on_complete(feed_id, guid),
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
