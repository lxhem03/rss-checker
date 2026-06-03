"""
RssCheckerTask — background loop that polls all saved RSS feeds.

- Runs every RSS_CHECK_INTERVAL seconds (60–300)
- For each feed, checks for new entries (by GUID / link)
- New entries are enqueued as download jobs
- Duplicate episodes are skipped via DB dedup
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import urlparse

import feedparser

from config import RSS_CHECK_INTERVAL, AUTH_GROUPS
from database import Database

logger = logging.getLogger(__name__)


class RssCheckerTask:
    def __init__(self, app, db: Database) -> None:
        self._app = app
        self._db = db
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="rss-checker")
        logger.info("RSS checker started (interval=%ds)", RSS_CHECK_INTERVAL)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    # ── Main loop ─────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while True:
            try:
                await self._check_all_feeds()
            except Exception:
                logger.exception("Unhandled error in RSS checker loop")
            await asyncio.sleep(RSS_CHECK_INTERVAL)

    async def _check_all_feeds(self) -> None:
        feeds = await self._db.get_all_feeds()
        if not feeds:
            return

        logger.debug("Checking %d feed(s)…", len(feeds))

        # Run all feed checks concurrently
        await asyncio.gather(
            *[self._check_feed(feed) for feed in feeds],
            return_exceptions=True,
        )

    async def _check_feed(self, feed: dict) -> None:
        feed_url: str = feed["feed_url"]
        title: str = feed["title"]
        user_id: int = feed["user_id"]
        feed_id = feed["_id"]
        seen_guids: list = feed.get("seen_guids", [])

        try:
            parsed = await asyncio.get_event_loop().run_in_executor(
                None, feedparser.parse, feed_url
            )
        except Exception as exc:
            logger.warning("Failed to fetch feed %s: %s", feed_url, exc)
            return

        new_entries = []
        for entry in parsed.entries:
            guid = entry.get("id") or entry.get("link") or entry.get("title", "")
            if guid and guid not in seen_guids:
                new_entries.append((guid, entry))

        if not new_entries:
            return

        logger.info("Feed '%s': %d new entry/entries found", title, len(new_entries))

        for guid, entry in new_entries:
            # Mark seen immediately so a crash won't re-queue it
            await self._db.mark_guid_seen(feed_id, guid)

            torrent_url = _extract_torrent_link(entry)
            if not torrent_url:
                logger.warning("No torrent link in entry '%s', skipping", entry.get("title", guid))
                continue

            await self._enqueue_rss_download(
                torrent_url=torrent_url,
                title=title,
                user_id=user_id,
                entry_title=entry.get("title", ""),
            )

    async def _enqueue_rss_download(
        self,
        torrent_url: str,
        title: str,
        user_id: int,
        entry_title: str,
    ) -> None:
        """
        Send a notification to the first AUTH_GROUP and enqueue via DownloadManager.
        """
        if not AUTH_GROUPS:
            logger.error("No AUTH_GROUPS configured — cannot send RSS notification")
            return

        group_id = AUTH_GROUPS[0]

        try:
            notify_msg = await self._app.send_message(
                chat_id=group_id,
                text=(
                    f"📡 <b>New RSS entry detected!</b>\n"
                    f"🏷️ <b>Show:</b> {title}\n"
                    f"📄 <b>Entry:</b> <code>{entry_title}</code>\n"
                    f"🔗 <b>URL:</b> <code>{torrent_url}</code>\n"
                    f"⏳ Queueing download…"
                ),
            )
        except Exception as exc:
            logger.error("Could not send notification to group %s: %s", group_id, exc)
            return

        mgr = self._app.download_manager

        # Build a fake minimal message-like object so DownloadManager can reply
        class _FakeMessage:
            chat = type("C", (), {"id": group_id})()
            from_user = type("U", (), {"id": user_id})()
            text = f"/download {torrent_url} -title {title}"

            async def reply_text(self, text, **kw):
                try:
                    return await self._app.send_message(chat_id=group_id, text=text)
                except Exception:
                    return notify_msg

            _app = self._app

        # Directly enqueue without going through the handler
        import uuid, os
        from config import DOWNLOAD_DIR
        from bot.utils.torrent import download_torrent
        from bot.utils.uploader import upload_file
        from bot.utils.episode import extract_season_episode
        import shutil

        job_id = uuid.uuid4().hex[:8]
        job_dir = os.path.join(DOWNLOAD_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)

        async def _run():
            try:
                last_edit_time = [0.0]
                import time

                async def on_progress(pct, speed, eta):
                    import time as _t
                    now = _t.monotonic()
                    if now - last_edit_time[0] < 4:
                        return
                    last_edit_time[0] = now
                    bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
                    try:
                        await notify_msg.edit_text(
                            f"🆔 <b>RSS Job:</b> <code>{job_id}</code>\n"
                            f"⬇️ <b>Downloading:</b> {title}\n"
                            f"📄 {entry_title}\n"
                            f"{bar} {pct:.1f}%\n"
                            f"⚡ {speed} | ⏱ ETA: {eta}"
                        )
                    except Exception:
                        pass

                all_files = await download_torrent(
                    source=torrent_url,
                    dest_dir=job_dir,
                    on_progress=on_progress,
                )

                video_exts = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}
                video_files = sorted([
                    f for f in all_files
                    if os.path.splitext(f)[1].lower() in video_exts
                ])

                if not video_files:
                    try:
                        await notify_msg.edit_text(
                            f"⚠️ No video files found for <b>{title}</b> — <code>{entry_title}</code>"
                        )
                    except Exception:
                        pass
                    return

                try:
                    await notify_msg.edit_text(
                        f"✅ <b>Download complete!</b>\n"
                        f"🏷️ {title} | {len(video_files)} file(s)\n"
                        f"📤 Uploading to PM…"
                    )
                except Exception:
                    pass

                db = self._app.db
                upload_tasks = []
                for vf in video_files:
                    _, episode = extract_season_episode(os.path.basename(vf))
                    ep_key = str(episode) if episode else os.path.basename(vf)

                    if await db.is_duplicate(title, ep_key):
                        logger.info("RSS dedup skip: %s %s", title, ep_key)
                        continue

                    file_status = await self._app.send_message(
                        chat_id=group_id,
                        text=f"📤 Uploading: <code>{os.path.basename(vf)}</code>",
                    )

                    async def _do_upload(vf=vf, ep_key=ep_key, file_status=file_status):
                        try:
                            await upload_file(
                                client=self._app,
                                video_path=vf,
                                title=title,
                                requesting_user_id=user_id,
                                status_message=file_status,
                            )
                            await db.mark_downloaded(title, ep_key, user_id)
                        except Exception as exc:
                            logger.exception("RSS upload failed: %s", vf)
                            try:
                                await file_status.edit_text(f"❌ Upload failed: <code>{exc}</code>")
                            except Exception:
                                pass

                    upload_tasks.append(_do_upload())

                if upload_tasks:
                    await asyncio.gather(*upload_tasks, return_exceptions=True)

            except Exception as exc:
                logger.exception("RSS job %s failed", job_id)
                try:
                    await notify_msg.edit_text(
                        f"❌ <b>RSS download failed</b> for <b>{title}</b>:\n<code>{exc}</code>"
                    )
                except Exception:
                    pass
            finally:
                shutil.rmtree(job_dir, ignore_errors=True)

        asyncio.create_task(_run(), name=f"rss-{job_id}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_torrent_link(entry: dict) -> Optional[str]:
    """
    Try to find a .torrent or magnet link from a feedparser entry.
    Checks: enclosures, links, entry link itself.
    """
    # 1. Enclosures (most common in Nyaa)
    for enc in entry.get("enclosures", []):
        href = enc.get("href") or enc.get("url", "")
        if href and (_is_torrent(href) or href.startswith("magnet:")):
            return href

    # 2. links array
    for lnk in entry.get("links", []):
        href = lnk.get("href", "")
        if href and (_is_torrent(href) or href.startswith("magnet:")):
            return href

    # 3. Direct entry link (Nyaa sometimes puts .torrent here)
    link = entry.get("link", "")
    if link and (_is_torrent(link) or link.startswith("magnet:")):
        return link

    # 4. nyaa_magnetlink custom tag (Nyaa RSS extension)
    magnet = entry.get("nyaa_magnetlink", "")
    if magnet:
        return magnet

    return None


def _is_torrent(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".torrent")
