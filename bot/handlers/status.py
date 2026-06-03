"""
/status — show all ongoing downloads in this session.
"""
from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from .auth import auth_only


def register(app: Client) -> None:

    @app.on_message(filters.command("status"))
    @auth_only
    async def status_handler(client: Client, message: Message):
        mgr = client.download_manager
        jobs = mgr.get_active_jobs()

        if not jobs:
            await message.reply_text("✅ No active downloads right now.", quote=True)
            return

        lines = ["<b>⬇️ Active downloads:</b>\n"]
        for job in jobs:
            bar = _progress_bar(job["progress"])
            lines.append(
                f"<b>ID:</b> <code>{job['id']}</code>\n"
                f"  📌 <b>{job['title']}</b>\n"
                f"  {bar} {job['progress']:.1f}%\n"
                f"  ⚡ {job['speed']}\n"
            )

        await message.reply_text("\n".join(lines), quote=True)


def _progress_bar(pct: float, width: int = 10) -> str:
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)
