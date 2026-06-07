"""
/rssfeed handler.

Usage:
    /rssfeed <feed_url> -title <Title> [-replace orig:new] [-avoid kw1,kw2]

-replace and -avoid are stored in MongoDB so they apply automatically
on every future RSS check without re-entering.

On first add, all currently-existing GUIDs are snapshotted so the bot
never downloads the backlog.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import feedparser

from pyrogram import Client, filters
from pyrogram.types import Message

from .auth import group_only
from bot.utils.arg_parser import parse_args

logger = logging.getLogger(__name__)

_USAGE = (
    "⚠️ <b>Usage:</b>\n"
    "<code>/rssfeed &lt;feed_url&gt; -title My Show</code>\n\n"
    "<b>Optional flags:</b>\n"
    "  <code>-replace original:replacement</code>  (repeatable)\n"
    "  <code>-avoid keyword1,keyword2</code>        (repeatable)\n\n"
    "<b>Examples:</b>\n"
    "<code>/rssfeed https://nyaa.si/... -title Diamond no Ace "
    "-replace Act II Second Season:S04</code>\n"
    "<code>/rssfeed https://nyaa.si/... -title Yozakura -avoid REPACK,v2</code>"
)


def register(app: Client) -> None:

    @app.on_message(filters.command("rssfeed"))
    @group_only
    async def rssfeed_handler(client: Client, message: Message):
        raw       = message.text or ""
        args_text = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""
        args      = parse_args(args_text)

        if not args.source:
            await message.reply_text(_USAGE, quote=True)
            return

        if not args.title:
            await message.reply_text(
                "⚠️ You must provide <code>-title</code>.\n\n" + _USAGE,
                quote=True,
            )
            return

        db      = client.db
        user_id = message.from_user.id

        existing = await db.feeds.find_one({"feed_url": args.source, "user_id": user_id})
        if existing:
            await message.reply_text(
                f"ℹ️ Feed already registered for <b>{existing['title']}</b>.",
                quote=True,
            )
            return

        status = await message.reply_text("⏳ Fetching feed to snapshot current entries…", quote=True)

        try:
            parsed = await asyncio.get_event_loop().run_in_executor(
                None, feedparser.parse, args.source
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

        existing_guids = []
        for entry in parsed.entries:
            guid = entry.get("id") or entry.get("link") or entry.get("title", "")
            if guid:
                existing_guids.append(guid)

        doc = {
            "feed_url":       args.source,
            "title":          args.title,
            "user_id":        user_id,
            "added_at":       datetime.now(timezone.utc),
            "seen_guids":     existing_guids,
            # ── Stored per-feed so they apply on every future check ───────
            "replacements":   [[o, r] for o, r in args.replacements],
            "avoid_keywords": args.avoid_keywords,
        }
        await db.feeds.insert_one(doc)

        # Build confirmation detail lines
        extra = ""
        if args.replacements:
            pairs = ", ".join(f"<code>{o}</code> → <code>{r}</code>" for o, r in args.replacements)
            extra += f"\n🔁 <b>Replace:</b> {pairs}"
        if args.avoid_keywords:
            kws = ", ".join(f"<code>{k}</code>" for k in args.avoid_keywords)
            extra += f"\n🚫 <b>Avoid:</b> {kws}"

        await status.edit_text(
            f"✅ <b>RSS feed added!</b>\n\n"
            f"📡 <b>Feed:</b> <code>{args.source}</code>\n"
            f"🏷️ <b>Title:</b> {args.title}\n"
            f"📦 <b>Existing entries skipped:</b> {len(existing_guids)}"
            f"{extra}\n\n"
            f"<i>Only new episodes after this moment will be downloaded.</i>"
        )
