"""
/feeds   — paginated list of active RSS feeds
/removefeed <url|#number> — remove a feed by URL or list number
"""
from __future__ import annotations

import math

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .auth import auth_only

_PAGE_SIZE = 8   # feeds per page — keeps each message well under Telegram's limit


def _build_page(feeds: list, page: int, total_pages: int) -> tuple[str, InlineKeyboardMarkup]:
    start = page * _PAGE_SIZE
    slice_ = feeds[start: start + _PAGE_SIZE]

    lines = [f"<b>📡 Your RSS feeds</b>  <i>(page {page + 1}/{total_pages})</i>\n"]
    for i, f in enumerate(slice_, start=start + 1):
        repl  = f.get("replacements", [])
        avoid = f.get("avoid_keywords", [])
        extra = ""
        if repl:
            pairs = ", ".join(f"{o}→{r}" for o, r in repl)
            extra += f"\n     🔁 <i>{pairs}</i>"
        if avoid:
            extra += f"\n     🚫 <i>avoid: {', '.join(avoid)}</i>"
        lines.append(
            f"{i}. <b>{f['title']}</b>{extra}\n"
            f"   <code>{f['feed_url']}</code>"
        )

    text = "\n\n".join(lines)

    # Navigation buttons
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"feeds:page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"feeds:page:{page + 1}"))

    kb_rows = [nav] if nav else []
    kb_rows.append([InlineKeyboardButton("❌ Close", callback_data="feeds:close")])
    return text, InlineKeyboardMarkup(kb_rows)


def register(app: Client) -> None:

    @app.on_message(filters.command("feeds"))
    @auth_only
    async def feeds_handler(client: Client, message: Message):
        feeds = await client.db.get_user_feeds(message.from_user.id)
        if not feeds:
            await message.reply_text("📭 You have no active RSS feeds.", quote=True)
            return

        total_pages = max(1, math.ceil(len(feeds) / _PAGE_SIZE))
        text, kb = _build_page(feeds, 0, total_pages)
        await message.reply_text(text, reply_markup=kb, quote=True)

    @app.on_callback_query(filters.regex(r"^feeds:"))
    @auth_only
    async def feeds_callback(client: Client, cq: CallbackQuery):
        data = cq.data

        if data == "feeds:close":
            try:
                await cq.message.delete()
            except Exception:
                pass
            await cq.answer()
            return

        if data.startswith("feeds:page:"):
            page = int(data.split(":")[-1])
            feeds = await client.db.get_user_feeds(cq.from_user.id)
            if not feeds:
                await cq.answer("No feeds found.")
                return
            total_pages = max(1, math.ceil(len(feeds) / _PAGE_SIZE))
            page = max(0, min(page, total_pages - 1))
            text, kb = _build_page(feeds, page, total_pages)
            await cq.edit_message_text(text, reply_markup=kb)
            await cq.answer()

    @app.on_message(filters.command("removefeed"))
    @auth_only
    async def removefeed_handler(client: Client, message: Message):
        raw   = message.text or ""
        parts = raw.split(None, 1)
        if len(parts) < 2:
            await message.reply_text(
                "⚠️ Usage:\n"
                "  <code>/removefeed &lt;feed_url&gt;</code>\n"
                "  <code>/removefeed #3</code>  (number from /feeds list)",
                quote=True,
            )
            return

        arg = parts[1].strip()
        db  = client.db

        # Support removal by #number from the /feeds list
        if arg.startswith("#"):
            try:
                idx = int(arg[1:]) - 1   # 1-based → 0-based
            except ValueError:
                await message.reply_text("❌ Invalid number.", quote=True)
                return
            feeds = await db.get_user_feeds(message.from_user.id)
            if idx < 0 or idx >= len(feeds):
                await message.reply_text(
                    f"❌ Number out of range. You have {len(feeds)} feed(s).", quote=True
                )
                return
            feed_url = feeds[idx]["feed_url"]
            feed_title = feeds[idx]["title"]
        else:
            feed_url   = arg
            feed_title = arg

        removed = await db.remove_feed(message.from_user.id, feed_url)
        if removed:
            await message.reply_text(
                f"🗑️ <b>Feed removed:</b> {feed_title}\n<code>{feed_url}</code>",
                quote=True,
            )
        else:
            await message.reply_text("❌ Feed not found.", quote=True)
