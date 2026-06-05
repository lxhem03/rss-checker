import asyncio
import logging
import os

from pyrogram import Client, utils
from pyrogram.enums import ParseMode

from config import API_ID, API_HASH, BOT_TOKEN, WORKERS, DOWNLOAD_DIR
from bot.handlers import register_handlers
from bot.tasks.rss_checker import RssCheckerTask
from bot.utils.downloader import DownloadManager
from database import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

utils.MIN_CHAT_ID = -999999999999
utils.MIN_CHANNEL_ID = -100999999999999


async def main() -> None:
    # ── Database first — fail fast if URI is wrong ─────────────────────────
    db = Database()
    try:
        await db.connect()
    except Exception as exc:
        logger.critical(
            "Could not connect to MongoDB: %s\n"
            "Check that MONGO_URI is set correctly in your Heroku config vars.\n"
            "It should look like: mongodb+srv://user:pass@cluster.mongodb.net/",
            exc,
        )
        raise SystemExit(1)

    # ── Pyrogram client ────────────────────────────────────────────────────
    app = Client(
        name="rss_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        workers=WORKERS,
        parse_mode=ParseMode.HTML,
    )

    app.db = db
    app.download_manager = None  # filled after client starts

    register_handlers(app)

    async with app:
        app.download_manager = DownloadManager(app)

        rss_task = RssCheckerTask(app, db)
        await rss_task.start()

        logger.info("✅ Bot is live.")
        await asyncio.Event().wait()   # run forever


if __name__ == "__main__":
    asyncio.run(main())
