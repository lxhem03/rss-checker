"""
/download handler.

Usage:
    /download <magnet|url> -title <Title> [-replace orig:new] [-avoid kw]
    /download -title <Title>   (reply to .torrent file)

If a .torrent URL returns 504 after retries, the error message tells the
user to try the magnet link instead.
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
    "  <code>-avoid keyword1,keyword2</code>\n\n"
    "💡 <b>Tip:</b> If a <code>.torrent</code> URL returns a timeout error, "
    "use the magnet link instead — it bypasses the download server entirely."
)


def register(app: Client) -> None:

    @app.on_message(filters.command("download"))
    @group_only
    async def download_handler(client: Client, message: Message):
        raw       = message.text or ""
        args_text = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""
        args      = parse_args(args_text)

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

        # Warn the user upfront if they gave a .torrent URL (not magnet)
        # so they know what to do if a 504 occurs
        source = args.source or ""
        if source.startswith(("http://", "https://")) and ".torrent" in source:
            await message.reply_text(
                "ℹ️ Fetching <code>.torrent</code> file… "
                "If this times out, cancel and retry with the magnet link.",
                quote=True,
            )

        await client.download_manager.enqueue(
            client=client,
            message=message,
            title=args.title,
            source=args.source,
            torrent_file_id=torrent_file_id,
            replacements=args.replacements,
            avoid_keywords=[],
        )
