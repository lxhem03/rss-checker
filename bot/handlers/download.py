"""
/download handler.

Usage:
    /download <magnet|url> -title <Title> [-replace orig:new] [-avoid kw1,kw2]
    /download -title <Title> [-replace orig:new]   (reply to .torrent file)

-replace  applied to filename before season/episode extraction
-avoid    for /download this filters nothing (you chose the torrent yourself)
          but is accepted and silently ignored so the syntax is consistent
"""
from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.types import Message

from .auth import group_only
from bot.utils.arg_parser import parse_args

logger = logging.getLogger(__name__)

_USAGE = (
    "⚠️ <b>Usage:</b>\n"
    "<code>/download &lt;magnet|url&gt; -title My Show</code>\n\n"
    "<b>Optional flags:</b>\n"
    "  <code>-replace original:replacement</code>  (repeatable)\n"
    "  <code>-avoid keyword1,keyword2</code>        (repeatable)"
)


def register(app: Client) -> None:

    @app.on_message(filters.command("download"))
    @group_only
    async def download_handler(client: Client, message: Message):
        raw       = message.text or ""
        args_text = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""
        args      = parse_args(args_text)

        # Handle reply-to-.torrent-file
        torrent_file_id = None
        if message.reply_to_message and message.reply_to_message.document:
            doc = message.reply_to_message.document
            if doc.file_name and doc.file_name.endswith(".torrent"):
                torrent_file_id = doc.file_id

        if not args.title:
            await message.reply_text(_USAGE, quote=True)
            return

        if not args.source and not torrent_file_id:
            await message.reply_text(
                "⚠️ No source provided. Give a magnet link, direct URL, "
                "or reply to a <code>.torrent</code> file.",
                quote=True,
            )
            return

        await client.download_manager.enqueue(
            client=client,
            message=message,
            title=args.title,
            source=args.source,
            torrent_file_id=torrent_file_id,
            replacements=args.replacements,
            avoid_keywords=[],          # not meaningful for manual /download
        )
