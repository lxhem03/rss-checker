"""
/feeds   — list active RSS feeds for the requesting user
/removefeed <url> — remove a feed
"""
from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from .auth import auth_only


def register(app: Client) -> None:

    @app.on_message(filters.command("feeds"))
    @auth_only
    async def feeds_handler(client: Client, message: Message):
        db = client.db
        feeds = await db.get_user_feeds(message.from_user.id)

        if not feeds:
            await message.reply_text("📭 You have no active RSS feeds.", quote=True)
            return

        lines = ["<b>📡 Your active RSS feeds:</b>\n"]
        for i, f in enumerate(feeds, 1):
            lines.append(f"{i}. <b>{f['title']}</b>\n   <code>{f['feed_url']}</code>")

        await message.reply_text("\n".join(lines), quote=True)

    @app.on_message(filters.command("removefeed"))
    @auth_only
    async def removefeed_handler(client: Client, message: Message):
        raw = message.text or ""
        parts = raw.split(None, 1)
        if len(parts) < 2:
            await message.reply_text(
                "⚠️ Usage: <code>/removefeed &lt;feed_url&gt;</code>", quote=True
            )
            return

        feed_url = parts[1].strip()
        db = client.db
        removed = await db.remove_feed(message.from_user.id, feed_url)
        if removed:
            await message.reply_text(f"🗑️ Feed removed:\n<code>{feed_url}</code>", quote=True)
        else:
            await message.reply_text("❌ Feed not found.", quote=True)
