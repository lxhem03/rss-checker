"""
/rssfeed handler.

Usage:
    /rssfeed <feed_url> -title <Title>

On first add, ALL currently existing GUIDs in the feed are immediately
marked as seen so the bot only downloads episodes that appear AFTER the
feed was registered — never the backlog.
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

import feedparser

from pyrogram import Client, filters
from pyrogram.types import Message

from .auth import group_only

logger = logging.getLogger(__name__)

_TITLE_RE = re.compile(r"-title\s+(.+?)(?:\s+-\w|$)", re.IGNORECASE | re.DOTALL)


def register(app: Client) -> None:

    @app.on_message(filters.command("rssfeed"))
    @group_only
    async def rssfeed_handler(client: Client, message: Message):
        raw = message.text or ""
        args_text = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""

        title_match = _TITLE_RE.search(args_text)
        title = title_match.group(1).strip() if title_match else None
        feed_url = _TITLE_RE.sub("", args_text).strip()

        if not feed_url:
            await message.reply_text(
                "⚠️ <b>Usage:</b> <code>/rssfeed &lt;feed_url&gt; -title My Show</code>",
                quote=True,
            )
            return

        if not title:
            await message.reply_text(
                "⚠️ You must provide <code>-title</code> with the feed command.\n"
                "Example: <code>/rssfeed https://nyaa.si/?page=rss&amp;q=... -title Re:Zero</code>",
                quote=True,
            )
            return

        db = client.db

        # Check if already registered
        from database import Database
        existing = await db.feeds.find_one({"feed_url": feed_url, "user_id": message.from_user.id})
        if existing:
            await message.reply_text(
                f"ℹ️ Feed already registered for <b>{existing['title']}</b>.",
                quote=True,
            )
            return

        # ── Fetch the feed NOW to snapshot all current GUIDs ──────────────
        status = await message.reply_text(
            f"⏳ Fetching feed to snapshot current entries…", quote=True
        )

        try:
            parsed = await asyncio.get_event_loop().run_in_executor(
                None, feedparser.parse, feed_url
            )
        except Exception as exc:
            await status.edit_text(f"❌ Could not fetch feed:\n<code>{exc}</code>")
            return

        if not parsed.feed and not parsed.entries:
            await status.edit_text(
                "❌ The URL does not appear to be a valid RSS feed. "
                "Please double-check the link."
            )
            return

        # Collect every GUID that already exists in the feed right now
        existing_guids = []
        for entry in parsed.entries:
            guid = entry.get("id") or entry.get("link") or entry.get("title", "")
            if guid:
                existing_guids.append(guid)

        # ── Save feed with all current GUIDs pre-marked as seen ───────────
        from datetime import datetime, timezone
        doc = {
            "feed_url":   feed_url,
            "title":      title,
            "user_id":    message.from_user.id,
            "added_at":   datetime.now(timezone.utc),
            "seen_guids": existing_guids,   # ← backlog is immediately ignored
        }
        await db.feeds.insert_one(doc)

        entry_count = len(existing_guids)
        await status.edit_text(
            f"✅ <b>RSS feed added!</b>\n\n"
            f"📡 <b>Feed:</b> <code>{feed_url}</code>\n"
            f"🏷️ <b>Title:</b> {title}\n"
            f"📦 <b>Existing entries skipped:</b> {entry_count}\n\n"
            f"<i>Only new episodes aired after this moment will be downloaded.</i>"
        )
