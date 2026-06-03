"""
/download handler.

Usage:
    /download <magnet|http_url> -title <Title>
    /download -title <Title>   (while replying to a .torrent file)
"""
from __future__ import annotations

import logging
import re

from pyrogram import Client, filters
from pyrogram.types import Message

from .auth import group_only
from bot.utils.downloader import DownloadManager

logger = logging.getLogger(__name__)

# Matches the -title flag anywhere in the text
_TITLE_RE = re.compile(r"-title\s+(.+?)(?:\s+-\w|$)", re.IGNORECASE | re.DOTALL)


def _parse_args(text: str):
    """Return (source, title) from command text (everything after /download)."""
    title_match = _TITLE_RE.search(text)
    title = title_match.group(1).strip() if title_match else None

    # Remove the -title ... part to get the source
    source = _TITLE_RE.sub("", text).strip()
    return source or None, title


def register(app: Client) -> None:

    @app.on_message(filters.command("download"))
    @group_only
    async def download_handler(client: Client, message: Message):
        raw = message.text or ""
        # Strip the /download command prefix
        args_text = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""

        source, title = _parse_args(args_text)

        # Handle reply-to-torrent-file
        torrent_file_id = None
        if message.reply_to_message and message.reply_to_message.document:
            doc = message.reply_to_message.document
            if doc.file_name and doc.file_name.endswith(".torrent"):
                torrent_file_id = doc.file_id

        if not title:
            await message.reply_text(
                "⚠️ <b>Missing -title flag.</b>\n"
                "Usage: <code>/download &lt;magnet|url&gt; -title My Show</code>",
                quote=True,
            )
            return

        if not source and not torrent_file_id:
            await message.reply_text(
                "⚠️ No source provided. Give a magnet link, direct URL, "
                "or reply to a <code>.torrent</code> file.",
                quote=True,
            )
            return

        mgr: DownloadManager = client.download_manager
        await mgr.enqueue(
            client=client,
            message=message,
            title=title,
            source=source,
            torrent_file_id=torrent_file_id,
        )
