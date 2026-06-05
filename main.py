# ── uvloop + event loop setup — MUST happen before any pyrogram import ────────
try:
    import uvloop
    import asyncio
    uvloop.install()
    # Explicitly create and set a loop so Pyrogram's sync.py import-time
    # call to asyncio.get_event_loop() finds one and doesn't raise.
    loop = uvloop.new_event_loop()
    asyncio.set_event_loop(loop)
except ImportError:
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

import logging
import os

from pyrogram import Client
from pyrogram.enums import ParseMode

from config import (
    API_ID, API_HASH, BOT_TOKEN,
    WORKERS, MAX_CONCURRENT_TRANSMISSIONS,
    DOWNLOAD_DIR,
)
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
    # ── Database — fail fast if URI is wrong ──────────────────────────────
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

    # ── Pyrogram client ───────────────────────────────────────────────────
    app = Client(
        name="rss_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        workers=WORKERS,
        max_concurrent_transmissions=MAX_CONCURRENT_TRANSMISSIONS,
        parse_mode=ParseMode.HTML,
    )

    app.db = db
    app.download_manager = None

    register_handlers(app)

    async with app:
        app.download_manager = DownloadManager(app)

        rss_task = RssCheckerTask(app, db)
        await rss_task.start()

        loop_name = type(asyncio.get_event_loop()).__name__
        logger.info("✅ Bot is live. Event loop: %s", loop_name)
        await asyncio.Event().wait()


if __name__ == "__main__":
    loop.run_until_complete(main())
