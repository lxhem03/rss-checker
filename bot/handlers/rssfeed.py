"""
/rssfeed handler.

Usage:
    /rssfeed <feed_url> -title <Title>
"""
from __future__ import annotations

import logging
import re

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
                "⚠️ You must provide <code>-title</code> with the feed command.",
                quote=True,
            )
            return

        db = client.db
        doc = await db.add_feed(
            user_id=message.from_user.id,
            feed_url=feed_url,
            title=title,
        )

        if doc.get("seen_guids") is not None and len(doc.get("seen_guids", [])) == 0 and doc.get("added_at"):
            # Freshly created
            await message.reply_text(
                f"✅ <b>RSS feed added!</b>\n\n"
                f"📡 <b>Feed:</b> <code>{feed_url}</code>\n"
                f"🏷️ <b>Title:</b> {title}\n\n"
                f"<i>The bot will check every ~2 minutes and download new episodes automatically.</i>",
                quote=True,
            )
        else:
            await message.reply_text(
                f"ℹ️ Feed already exists for <b>{title}</b>.",
                quote=True,
            )
