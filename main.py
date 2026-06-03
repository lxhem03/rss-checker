import asyncio
import logging
import os

from pyrogram import Client
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


async def main() -> None:
    db = Database()
    await db.connect()

    app = Client(
        name="rss_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        workers=WORKERS,
        parse_mode=ParseMode.HTML,
    )

    # Attach shared objects to app so handlers can reach them
    app.db = db

    # DownloadManager needs the client reference; we create it before starting
    # and re-attach after start so it holds the live client.
    app.download_manager = None  # placeholder

    register_handlers(app)

    async with app:
        # Now the client is running — attach real manager
        app.download_manager = DownloadManager(app)

        rss_task = RssCheckerTask(app, db)
        await rss_task.start()

        logger.info("✅ Bot is live.")
        await asyncio.Event().wait()   # block forever


if __name__ == "__main__":
    asyncio.run(main())
