"""
feed_fetch

Shared helper for fetching + parsing RSS feeds.

Why this exists
────────────────
feedparser.parse(url) — when given a URL string — opens the connection
itself via urllib with NO timeout. If a feed host (e.g. nyaa.si) stalls,
rate-limits, or drops a connection without closing it, the calling thread
blocks forever. Python threads cannot be killed once started, so a single
hung feed permanently eats one worker out of the ThreadPoolExecutor.

Since the RSS checker re-polls every cycle, this compounds: each cycle
can wedge another worker on another slow/unlucky feed, and after enough
cycles the entire pool is exhausted — every feed check just stops, with
no error, because there are no free workers left to run any of them.

Fix: fetch the raw bytes ourselves with an explicit, bounded timeout
(httpx), then hand the bytes to feedparser.parse() — which does no I/O
when given bytes instead of a URL, so it can't hang. Any slow/dead feed
now fails fast and frees its worker instead of blocking it forever.

504s from nyaa.si
─────────────────
feedparser.parse(url) used to swallow HTTP errors silently (a 504 just
came back as an empty, non-crashing feed — bozo=1 — so transient 504s
from nyaa.si, which is known to return them under load on its RSS/
search endpoints, were always happening but invisible). Fetching via
httpx + raise_for_status() surfaces that same, pre-existing 504 as a
real exception instead of hiding it — so we need our own retry here,
the same way bot/utils/torrent.py's old .torrent-URL fetch already
retried nyaa's identical 504-under-load behavior.
"""
from __future__ import annotations

import logging
import time

import httpx
import feedparser

logger = logging.getLogger(__name__)

# (connect, read) bounds — generous enough for a slow feed host, but
# guarantees a worker thread always comes back well within a single
# RSS_CHECK_INTERVAL (min 60s), so hung fetches can never pile up.
FEED_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; rss-checker-bot/1.0)"}

MAX_FEED_FETCH_RETRIES = 3  # 2s, 4s, 8s backoff — well under a 60s+ poll interval


def fetch_feed_sync(feed_url: str) -> feedparser.FeedParserDict:
    """
    Blocking fetch+parse of a single feed URL. Safe to run inside a
    ThreadPoolExecutor worker — always returns or raises within a bounded
    time, never hangs indefinitely.

    Retries on 5xx responses and connection errors (exponential backoff),
    matching nyaa.si's known 504-under-load behavior. Raises after all
    retries are exhausted; callers should treat that like any other
    "feed fetch failed" case.
    """
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(1, MAX_FEED_FETCH_RETRIES + 1):
        try:
            resp = httpx.get(
                feed_url,
                timeout=FEED_HTTP_TIMEOUT,
                follow_redirects=True,
                headers=_HEADERS,
            )
            resp.raise_for_status()
            return feedparser.parse(resp.content)

        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            last_exc = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # 4xx other than a few transient ones isn't going to fix itself
            # on retry (bad URL, 403, etc.) — fail fast instead of waiting.
            if status is not None and status < 500 and status != 429:
                raise
            wait = 2 ** attempt  # 2s, 4s, 8s
            logger.warning(
                "Feed fetch attempt %d/%d failed for %s (status=%s): %s — retrying in %ds",
                attempt, MAX_FEED_FETCH_RETRIES, feed_url, status, exc, wait,
            )
            if attempt < MAX_FEED_FETCH_RETRIES:
                time.sleep(wait)  # blocking sleep is fine — we're already in a worker thread

    raise last_exc
