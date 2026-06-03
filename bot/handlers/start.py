from pyrogram import Client, filters
from pyrogram.types import Message


def register(app: Client) -> None:

    @app.on_message(filters.command("start"))
    async def start_handler(_: Client, message: Message):
        await message.reply_text(
            "👋 <b>Welcome to RSS Torrent Bot!</b>\n\n"
            "<b>Available commands (authorised users only):</b>\n"
            "• <code>/download &lt;magnet|url&gt; -title &lt;Title&gt;</code>\n"
            "• <code>/rssfeed &lt;feed_url&gt; -title &lt;Title&gt;</code>\n"
            "• <code>/feeds</code> — list your active RSS feeds\n"
            "• <code>/removefeed &lt;feed_url&gt;</code> — remove a feed\n"
            "• <code>/status</code> — view ongoing downloads\n"
            "• <code>/cancel &lt;id&gt;</code> — cancel a download\n\n"
            "<i>Commands must be used in an authorised group.</i>",
            quote=True,
        )
