"""
/settings — per-user settings with inline button navigation.

_AWAITING stores (state_constant, original_bot_message) per user_id so
that after the user sends their input we can edit the original settings
message back to the correct sub-page instead of sending a new reply.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, Optional, Tuple

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .auth import auth_only

logger = logging.getLogger(__name__)

# ── In-memory awaiting state per user ─────────────────────────────────────────
# value: (state_str, original_settings_Message)
_AWAITING: Dict[int, Tuple[str, Message]] = {}

# ── Callback-data constants ───────────────────────────────────────────────────
_MAIN        = "cfg:main"
_DUMP        = "cfg:dump"
_DUMP_SET    = "cfg:dump_set"
_DUMP_DEL    = "cfg:dump_del"
_UPLOAD      = "cfg:upload"
_UPLOAD_SET  = "cfg:upload_set"
_UPLOAD_DEL  = "cfg:upload_del"
_RENAME      = "cfg:rename"
_RENAME_SET  = "cfg:rename_set"
_RENAME_DEL  = "cfg:rename_del"
_REGEX       = "cfg:regex"
_REGEX_ADD   = "cfg:regex_add"
_REGEX_DEL   = "cfg:regex_del:"   # prefix — suffix is index
_PM_TOGGLE   = "cfg:pm_toggle"


# ── Keyboard builders ─────────────────────────────────────────────────────────

def _kb_main(s: dict) -> InlineKeyboardMarkup:
    pm = "✅ PM Forward ON" if s.get("forward_to_pm", True) else "❌ PM Forward OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📥 Dump Channel",    callback_data=_DUMP),
            InlineKeyboardButton("📤 Upload Channel",  callback_data=_UPLOAD),
        ],
        [
            InlineKeyboardButton("✏️ Rename Template", callback_data=_RENAME),
            InlineKeyboardButton("🔣 Custom Regex",    callback_data=_REGEX),
        ],
        [
            InlineKeyboardButton(pm, callback_data=_PM_TOGGLE),
        ],
    ])


def _kb_channel(kind: str, has_value: bool) -> InlineKeyboardMarkup:
    set_cb = _DUMP_SET if kind == "dump" else _UPLOAD_SET
    del_cb = _DUMP_DEL if kind == "dump" else _UPLOAD_DEL
    rows   = [[InlineKeyboardButton("✏️ Set / Change", callback_data=set_cb)]]
    if has_value:
        rows.append([InlineKeyboardButton("🗑 Delete", callback_data=del_cb)])
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=_MAIN)])
    return InlineKeyboardMarkup(rows)


def _kb_rename(has_value: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("✏️ Set / Change", callback_data=_RENAME_SET)]]
    if has_value:
        rows.append([InlineKeyboardButton("🗑 Reset to default", callback_data=_RENAME_DEL)])
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=_MAIN)])
    return InlineKeyboardMarkup(rows)


def _kb_regex(patterns: list) -> InlineKeyboardMarkup:
    rows = []
    for i, p in enumerate(patterns):
        label = p[:30] + "…" if len(p) > 30 else p
        rows.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"{_REGEX_DEL}{i}")])
    rows.append([InlineKeyboardButton("➕ Add Pattern", callback_data=_REGEX_ADD)])
    rows.append([InlineKeyboardButton("◀️ Back",        callback_data=_MAIN)])
    return InlineKeyboardMarkup(rows)


def _kb_waiting(back_cb: str) -> InlineKeyboardMarkup:
    """Shown while bot is waiting for user text input."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=back_cb)
    ]])


# ── Text builders ─────────────────────────────────────────────────────────────

def _fmt_ids(ids: list) -> str:
    return ", ".join(str(i) for i in ids) if ids else "None"


def _text_main(s: dict, mention: str) -> str:
    pm   = "✅ On" if s.get("forward_to_pm", True) else "❌ Off"
    tmpl = s.get("rename_template") or "<i>default</i>"
    n_re = len(s.get("custom_patterns", []))
    return (
        f"⚙️ <b>Settings for</b> {mention}\n\n"
        f"📥 <b>Dump Channel:</b> <code>{_fmt_ids(s.get('dump_channels', []))}</code>\n"
        f"📤 <b>Upload Channel:</b> <code>{_fmt_ids(s.get('upload_channels', []))}</code>\n"
        f"✏️ <b>Rename Template:</b> <code>{tmpl}</code>\n"
        f"🔣 <b>Custom Regex:</b> {n_re} pattern(s)\n"
        f"📨 <b>Forward to PM:</b> {pm}"
    )


def _text_dump(s: dict) -> str:
    return (
        f"📥 <b>Dump Channel</b>\n\n"
        f"Current: <code>{_fmt_ids(s.get('dump_channels', []))}</code>\n\n"
        f"Files from <b>/download</b> will be sent here.\n\n"
        f"Select an option:"
    )


def _text_upload(s: dict) -> str:
    return (
        f"📤 <b>Upload Channel</b>\n\n"
        f"Current: <code>{_fmt_ids(s.get('upload_channels', []))}</code>\n\n"
        f"Files from <b>/rssfeed</b> will be sent here.\n\n"
        f"Select an option:"
    )


def _text_rename(s: dict) -> str:
    tmpl = s.get("rename_template") or "<i>not set — using default</i>"
    return (
        f"✏️ <b>Rename Template</b>\n\n"
        f"Current: <code>{tmpl}</code>\n\n"
        f"Placeholders:\n"
        f"  <code>{{title}}</code> — show title\n"
        f"  <code>{{season:02d}}</code> — season (zero-padded)\n"
        f"  <code>{{episode:02d}}</code> — episode (zero-padded)\n\n"
        f"Example: <code>{{title}} - S{{season:02d}}E{{episode:02d}}.mkv</code>\n\n"
        f"Select an option:"
    )


def _text_regex(patterns: list) -> str:
    body = (
        "\n".join(f"  {i+1}. <code>{p}</code>" for i, p in enumerate(patterns))
        if patterns else "<i>No custom patterns yet.</i>"
    )
    return (
        f"🔣 <b>Custom Regex Patterns</b>\n\n"
        f"Checked <b>before</b> built-in patterns.\n\n"
        f"{body}\n\n"
        f"<b>Format:</b>\n"
        f"  Episode only: <code>PATTERN</code>\n"
        f"  With season:  <code>PATTERN|season,episode</code>\n\n"
        f"Use plain groups <code>(\\d+)</code>, not named groups."
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _go_to_dump(msg: Message, s: dict) -> None:
    await msg.edit_text(_text_dump(s), reply_markup=_kb_channel("dump", bool(s.get("dump_channels"))))


async def _go_to_upload(msg: Message, s: dict) -> None:
    await msg.edit_text(_text_upload(s), reply_markup=_kb_channel("upload", bool(s.get("upload_channels"))))


async def _go_to_rename(msg: Message, s: dict) -> None:
    await msg.edit_text(_text_rename(s), reply_markup=_kb_rename(bool(s.get("rename_template"))))


async def _go_to_regex(msg: Message, s: dict) -> None:
    patterns = s.get("custom_patterns", [])
    await msg.edit_text(_text_regex(patterns), reply_markup=_kb_regex(patterns))


async def _go_to_main(msg: Message, s: dict, mention: str) -> None:
    await msg.edit_text(_text_main(s, mention), reply_markup=_kb_main(s))


# ── Register ──────────────────────────────────────────────────────────────────

def register(app: Client) -> None:

    @app.on_message(filters.command("settings"))
    @auth_only
    async def settings_cmd(client: Client, message: Message):
        s       = await client.db.get_settings(message.from_user.id)
        mention = message.from_user.mention
        await message.reply_text(
            _text_main(s, mention),
            reply_markup=_kb_main(s),
            quote=True,
        )

    # ── Callback router ───────────────────────────────────────────────────
    @app.on_callback_query(filters.regex(r"^cfg:"))
    async def settings_callback(client: Client, cq: CallbackQuery):
        uid     = cq.from_user.id
        data    = cq.data
        db      = client.db
        s       = await db.get_settings(uid)
        bot_msg = cq.message   # the message the buttons are on

        # ── Main page ─────────────────────────────────────────────────────
        if data == _MAIN:
            _AWAITING.pop(uid, None)
            await _go_to_main(bot_msg, s, cq.from_user.mention)

        # ── PM toggle ─────────────────────────────────────────────────────
        elif data == _PM_TOGGLE:
            new_val = not s.get("forward_to_pm", True)
            await db.update_setting(uid, "forward_to_pm", new_val)
            s["forward_to_pm"] = new_val
            await _go_to_main(bot_msg, s, cq.from_user.mention)
            await cq.answer("PM forwarding " + ("enabled ✅" if new_val else "disabled ❌"))
            return

        # ── Dump channel ──────────────────────────────────────────────────
        elif data == _DUMP:
            _AWAITING.pop(uid, None)
            await _go_to_dump(bot_msg, s)

        elif data == _DUMP_DEL:
            await db.update_setting(uid, "dump_channels", [])
            s["dump_channels"] = []
            await _go_to_dump(bot_msg, s)
            await cq.answer("Dump channel(s) removed.")
            return

        elif data == _DUMP_SET:
            _AWAITING[uid] = (_DUMP_SET, bot_msg)
            await bot_msg.edit_text(
                "📥 <b>Set Dump Channel(s)</b>\n\n"
                "Send one or more channel/group IDs separated by commas.\n\n"
                "<i>Example:</i> <code>-1001234567890, -1009876543210</code>\n\n"
                "⚠️ Make sure the bot is an <b>admin with all rights</b> "
                "in all channels/groups.",
                reply_markup=_kb_waiting(_DUMP),
            )

        # ── Upload channel ────────────────────────────────────────────────
        elif data == _UPLOAD:
            _AWAITING.pop(uid, None)
            await _go_to_upload(bot_msg, s)

        elif data == _UPLOAD_DEL:
            await db.update_setting(uid, "upload_channels", [])
            s["upload_channels"] = []
            await _go_to_upload(bot_msg, s)
            await cq.answer("Upload channel(s) removed.")
            return

        elif data == _UPLOAD_SET:
            _AWAITING[uid] = (_UPLOAD_SET, bot_msg)
            await bot_msg.edit_text(
                "📤 <b>Set Upload Channel(s)</b>\n\n"
                "Send one or more channel/group IDs separated by commas.\n\n"
                "<i>Example:</i> <code>-1001234567890, -1009876543210</code>\n\n"
                "⚠️ Make sure the bot is an <b>admin with all rights</b> "
                "in all channels/groups.",
                reply_markup=_kb_waiting(_UPLOAD),
            )

        # ── Rename template ───────────────────────────────────────────────
        elif data == _RENAME:
            _AWAITING.pop(uid, None)
            await _go_to_rename(bot_msg, s)

        elif data == _RENAME_DEL:
            await db.update_setting(uid, "rename_template", None)
            s["rename_template"] = None
            await _go_to_rename(bot_msg, s)
            await cq.answer("Template reset to default.")
            return

        elif data == _RENAME_SET:
            _AWAITING[uid] = (_RENAME_SET, bot_msg)
            await bot_msg.edit_text(
                "✏️ <b>Set Rename Template</b>\n\n"
                "Send your template string. Example:\n"
                "<code>{title} - S{season:02d}E{episode:02d}.mkv</code>\n\n"
                "Must contain <code>{title}</code> and <code>{episode}</code>.\n"
                "<code>{season}</code> is optional.",
                reply_markup=_kb_waiting(_RENAME),
            )

        # ── Custom regex ──────────────────────────────────────────────────
        elif data == _REGEX:
            _AWAITING.pop(uid, None)
            await _go_to_regex(bot_msg, s)

        elif data == _REGEX_ADD:
            _AWAITING[uid] = (_REGEX_ADD, bot_msg)
            await bot_msg.edit_text(
                "🔣 <b>Add Custom Regex Pattern</b>\n\n"
                "Send your pattern string.\n\n"
                "<b>Episode only (1 capture group):</b>\n"
                "  <code>[Ee]p?(\\d{1,4})</code>\n\n"
                "<b>Season + Episode (2 groups), pipe-separated spec:</b>\n"
                "  <code>[Ss](\\d{1,2})[Ee](\\d{1,3})|season,episode</code>\n\n"
                "Pattern is validated before saving.",
                reply_markup=_kb_waiting(_REGEX),
            )

        elif data.startswith(_REGEX_DEL):
            try:
                idx      = int(data[len(_REGEX_DEL):])
                patterns = s.get("custom_patterns", [])
                if 0 <= idx < len(patterns):
                    await db.remove_custom_pattern(uid, patterns[idx])
                    patterns.pop(idx)
                    s["custom_patterns"] = patterns
                    await _go_to_regex(bot_msg, s)
                    await cq.answer("Pattern removed.")
                    return
                else:
                    await cq.answer("Pattern not found.")
                    return
            except (ValueError, IndexError):
                await cq.answer("Error removing pattern.")
                return

        await cq.answer()

    # ── Message listener for text inputs ──────────────────────────────────
    @app.on_message(filters.text & ~filters.command(""))
    @auth_only
    async def settings_input(client: Client, message: Message):
        uid     = message.from_user.id
        waiting = _AWAITING.get(uid)
        if waiting is None:
            return

        state, bot_msg = waiting
        db   = client.db
        text = message.text.strip()

        # Delete the user's input message to keep the chat clean
        try:
            await message.delete()
        except Exception:
            pass

        # ── Channel IDs ───────────────────────────────────────────────────
        if state in (_DUMP_SET, _UPLOAD_SET):
            raw_ids = [x.strip() for x in text.replace(" ", "").split(",")]
            ids, bad = [], []
            for r in raw_ids:
                try:
                    ids.append(int(r))
                except ValueError:
                    bad.append(r)

            if bad:
                # Show error but keep state — user can try again
                await bot_msg.edit_text(
                    f"❌ Invalid ID(s): <code>{', '.join(bad)}</code>\n\n"
                    f"IDs must be integers like <code>-1001234567890</code>.\n"
                    f"Try again or press Cancel.",
                    reply_markup=_kb_waiting(_DUMP if state == _DUMP_SET else _UPLOAD),
                )
                return

            key  = "dump_channels"   if state == _DUMP_SET   else "upload_channels"
            back = _DUMP             if state == _DUMP_SET   else _UPLOAD
            await db.update_setting(uid, key, ids)
            _AWAITING.pop(uid, None)

            # Reload settings and go back to the channel sub-page
            s = await db.get_settings(uid)
            if state == _DUMP_SET:
                await _go_to_dump(bot_msg, s)
            else:
                await _go_to_upload(bot_msg, s)

        # ── Rename template ───────────────────────────────────────────────
        elif state == _RENAME_SET:
            if "{title}" not in text:
                await bot_msg.edit_text(
                    "❌ Template must contain <code>{title}</code>. Try again or press Cancel.",
                    reply_markup=_kb_waiting(_RENAME),
                )
                return
            if "{episode" not in text:
                await bot_msg.edit_text(
                    "❌ Template must contain <code>{episode}</code>. Try again or press Cancel.",
                    reply_markup=_kb_waiting(_RENAME),
                )
                return
            try:
                text.format(title="Test", season=1, episode=1)
            except (KeyError, ValueError) as exc:
                await bot_msg.edit_text(
                    f"❌ Invalid template: <code>{exc}</code>. Try again or press Cancel.",
                    reply_markup=_kb_waiting(_RENAME),
                )
                return

            await db.update_setting(uid, "rename_template", text)
            _AWAITING.pop(uid, None)
            s = await db.get_settings(uid)
            await _go_to_rename(bot_msg, s)

        # ── Custom regex ──────────────────────────────────────────────────
        elif state == _REGEX_ADD:
            if "|" in text:
                pattern_str, spec = text.split("|", 1)
                group_names = [g.strip() for g in spec.split(",")]
                invalid_names = [g for g in group_names if g not in ("season", "episode")]
                if invalid_names:
                    await bot_msg.edit_text(
                        f"❌ Group names must be <code>season</code> or <code>episode</code>. "
                        f"Got: <code>{', '.join(invalid_names)}</code>. Try again or Cancel.",
                        reply_markup=_kb_waiting(_REGEX),
                    )
                    return
            else:
                pattern_str = text
                group_names = ["episode"]

            try:
                compiled = re.compile(pattern_str)
            except re.error as exc:
                await bot_msg.edit_text(
                    f"❌ Invalid regex: <code>{exc}</code>. Try again or press Cancel.",
                    reply_markup=_kb_waiting(_REGEX),
                )
                return

            if compiled.groups != len(group_names):
                await bot_msg.edit_text(
                    f"❌ Pattern has <b>{compiled.groups}</b> capture group(s) "
                    f"but spec names <b>{len(group_names)}</b>. Try again or Cancel.",
                    reply_markup=_kb_waiting(_REGEX),
                )
                return

            await db.add_custom_pattern(uid, text)
            _AWAITING.pop(uid, None)
            s = await db.get_settings(uid)
            await _go_to_regex(bot_msg, s)
