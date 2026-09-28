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
"""
from __future__ import annotations

import httpx
import feedparser

# (connect, read) bounds — generous enough for a slow feed host, but
# guarantees a worker thread always comes back well within a single
# RSS_CHECK_INTERVAL (min 60s), so hung fetches can never pile up.
FEED_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; rss-checker-bot/1.0)"}


def fetch_feed_sync(feed_url: str) -> feedparser.FeedParserDict:
    """
    Blocking fetch+parse of a single feed URL. Safe to run inside a
    ThreadPoolExecutor worker — always returns or raises within
    FEED_HTTP_TIMEOUT, never hangs indefinitely.

    Raises on any HTTP/connection/timeout error; callers should catch
    and treat it the same as any other "feed fetch failed" case.
    """
    resp = httpx.get(
        feed_url,
        timeout=FEED_HTTP_TIMEOUT,
        follow_redirects=True,
        headers=_HEADERS,
    )
    resp.raise_for_status()
    return feedparser.parse(resp.content)
