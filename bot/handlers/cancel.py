"""
/cancel <job_id>  — cancel an active download
"""
from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from .auth import auth_only


def register(app: Client) -> None:

    @app.on_message(filters.command("cancel"))
    @auth_only
    async def cancel_handler(client: Client, message: Message):
        raw = message.text or ""
        parts = raw.split(None, 1)
        if len(parts) < 2:
            await message.reply_text(
                "⚠️ Usage: <code>/cancel &lt;job_id&gt;</code>", quote=True
            )
            return

        job_id = parts[1].strip()
        mgr = client.download_manager
        cancelled = await mgr.cancel(job_id)
        if cancelled:
            await message.reply_text(f"🛑 Download <code>{job_id}</code> cancelled.", quote=True)
        else:
            await message.reply_text(
                f"❌ No active download with ID <code>{job_id}</code>.", quote=True
            )
