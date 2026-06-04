"""
/settings — per-user settings with inline button navigation.

Page map
────────
  PAGE_MAIN          /settings entry point
  PAGE_DUMP          dump channel sub-menu
  PAGE_DUMP_SET      waiting for user to send channel IDs (dump)
  PAGE_UPLOAD        upload channel sub-menu
  PAGE_UPLOAD_SET    waiting for user to send channel IDs (upload)
  PAGE_RENAME        rename template sub-menu
  PAGE_RENAME_SET    waiting for user to send new template
  PAGE_REGEX         custom regex list
  PAGE_REGEX_ADD     waiting for user to send new regex
  PAGE_PM_TOGGLE     (handled inline, no separate page needed)

Conversation state is stored in memory (dict keyed by user_id) because
settings edits are short-lived and do not need to survive a restart.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Dict, Optional

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .auth import auth_only
from bot.utils.user_settings import get_patterns  # to validate regex

logger = logging.getLogger(__name__)

# ── In-memory "awaiting input" state per user ─────────────────────────────────
# value: one of the PAGE_* constants below
_AWAITING: Dict[int, str] = {}

# ── Page / callback-data constants ───────────────────────────────────────────
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
_CANCEL      = "cfg:cancel"


# ── Keyboard builders ─────────────────────────────────────────────────────────

def _kb_main(s: dict) -> InlineKeyboardMarkup:
    pm = "✅ PM Forward" if s.get("forward_to_pm", True) else "❌ PM Forward"
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
            InlineKeyboardButton(pm,                   callback_data=_PM_TOGGLE),
        ],
    ])


def _kb_channel(kind: str, has_value: bool) -> InlineKeyboardMarkup:
    set_cb = _DUMP_SET  if kind == "dump" else _UPLOAD_SET
    del_cb = _DUMP_DEL  if kind == "dump" else _UPLOAD_DEL
    rows = [[InlineKeyboardButton("✏️ Set / Change", callback_data=set_cb)]]
    if has_value:
        rows.append([InlineKeyboardButton("🗑 Delete", callback_data=del_cb)])
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=_MAIN)])
    return InlineKeyboardMarkup(rows)


def _kb_rename(has_value: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("✏️ Set / Change", callback_data=_RENAME_SET)]]
    if has_value:
        rows.append([InlineKeyboardButton("🗑 Delete (use default)", callback_data=_RENAME_DEL)])
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=_MAIN)])
    return InlineKeyboardMarkup(rows)


def _kb_regex(patterns: list) -> InlineKeyboardMarkup:
    rows = []
    for i, p in enumerate(patterns):
        label = p[:32] + "…" if len(p) > 32 else p
        rows.append([InlineKeyboardButton(
            f"🗑 {label}", callback_data=f"{_REGEX_DEL}{i}"
        )])
    rows.append([InlineKeyboardButton("➕ Add Pattern", callback_data=_REGEX_ADD)])
    rows.append([InlineKeyboardButton("◀️ Back",        callback_data=_MAIN)])
    return InlineKeyboardMarkup(rows)


def _kb_cancel(back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=back_cb)
    ]])


# ── Text builders ─────────────────────────────────────────────────────────────

def _fmt_ids(ids: list) -> str:
    return ", ".join(str(i) for i in ids) if ids else "None"


def _text_main(s: dict, mention: str) -> str:
    pm = "✅ On" if s.get("forward_to_pm", True) else "❌ Off"
    tmpl = s.get("rename_template") or "<i>default</i>"
    n_regex = len(s.get("custom_patterns", []))
    return (
        f"⚙️ <b>Settings for</b> {mention}\n\n"
        f"📥 <b>Dump Channel:</b> <code>{_fmt_ids(s.get('dump_channels', []))}</code>\n"
        f"📤 <b>Upload Channel:</b> <code>{_fmt_ids(s.get('upload_channels', []))}</code>\n"
        f"✏️ <b>Rename Template:</b> <code>{tmpl}</code>\n"
        f"🔣 <b>Custom Regex:</b> {n_regex} pattern(s)\n"
        f"📨 <b>Forward to PM:</b> {pm}"
    )


def _text_dump(s: dict) -> str:
    return (
        f"📥 <b>Dump Channel</b>\n\n"
        f"Current: <code>{_fmt_ids(s.get('dump_channels', []))}</code>\n\n"
        f"Select an option:"
    )


def _text_upload(s: dict) -> str:
    return (
        f"📤 <b>Upload Channel</b>\n\n"
        f"Current: <code>{_fmt_ids(s.get('upload_channels', []))}</code>\n\n"
        f"Select an option:"
    )


def _text_rename(s: dict) -> str:
    tmpl = s.get("rename_template") or "<i>not set — using default</i>"
    return (
        f"✏️ <b>Rename Template</b>\n\n"
        f"Current: <code>{tmpl}</code>\n\n"
        f"Available placeholders:\n"
        f"  <code>{{title}}</code> — show title\n"
        f"  <code>{{season:02d}}</code> — season (zero-padded)\n"
        f"  <code>{{episode:02d}}</code> — episode (zero-padded)\n\n"
        f"Example: <code>{{title}} - S{{season:02d}}E{{episode:02d}}.mkv</code>\n\n"
        f"Select an option:"
    )


def _text_regex(patterns: list) -> str:
    if not patterns:
        body = "<i>No custom patterns yet.</i>"
    else:
        lines = [f"  {i+1}. <code>{p}</code>" for i, p in enumerate(patterns)]
        body = "\n".join(lines)
    return (
        f"🔣 <b>Custom Regex Patterns</b>\n\n"
        f"These are checked <b>before</b> the built-in patterns.\n\n"
        f"{body}\n\n"
        f"<b>Format when adding:</b>\n"
        f"  Episode-only:  <code>PATTERN</code>\n"
        f"  With season:   <code>PATTERN|season,episode</code>\n\n"
        f"Use plain capture groups <code>(\\d+)</code>, not named groups.\n"
        f"Example: <code>[Ss](\\d{{1,2}})[Ee](\\d{{1,3}})|season,episode</code>"
    )


# ── Register ──────────────────────────────────────────────────────────────────

def register(app: Client) -> None:

    # ── /settings command ─────────────────────────────────────────────────
    @app.on_message(filters.command("settings") & filters.private | filters.command("settings"))
    @auth_only
    async def settings_cmd(client: Client, message: Message):
        s = await client.db.get_settings(message.from_user.id)
        mention = message.from_user.mention
        await message.reply_text(
            _text_main(s, mention),
            reply_markup=_kb_main(s),
            quote=True,
        )

    # ── Callback router ───────────────────────────────────────────────────
    @app.on_callback_query(filters.regex(r"^cfg:"))
    async def settings_callback(client: Client, cq: CallbackQuery):
        uid  = cq.from_user.id
        data = cq.data
        db   = client.db

        # Only the user who opened settings can interact with it
        s = await db.get_settings(uid)

        # ── Main page ─────────────────────────────────────────────────────
        if data == _MAIN:
            _AWAITING.pop(uid, None)
            mention = cq.from_user.mention
            await cq.edit_message_text(_text_main(s, mention), reply_markup=_kb_main(s))

        # ── PM toggle (no sub-page needed) ────────────────────────────────
        elif data == _PM_TOGGLE:
            new_val = not s.get("forward_to_pm", True)
            await db.update_setting(uid, "forward_to_pm", new_val)
            s["forward_to_pm"] = new_val
            mention = cq.from_user.mention
            await cq.edit_message_text(_text_main(s, mention), reply_markup=_kb_main(s))
            await cq.answer("PM forwarding " + ("enabled ✅" if new_val else "disabled ❌"))

        # ── Dump channel sub-menu ─────────────────────────────────────────
        elif data == _DUMP:
            _AWAITING.pop(uid, None)
            await cq.edit_message_text(
                _text_dump(s),
                reply_markup=_kb_channel("dump", bool(s.get("dump_channels"))),
            )

        elif data == _DUMP_DEL:
            await db.update_setting(uid, "dump_channels", [])
            s["dump_channels"] = []
            await cq.edit_message_text(
                _text_dump(s),
                reply_markup=_kb_channel("dump", False),
            )
            await cq.answer("Dump channel(s) removed.")

        elif data == _DUMP_SET:
            _AWAITING[uid] = _DUMP_SET
            await cq.edit_message_text(
                "📥 <b>Set Dump Channel(s)</b>\n\n"
                "Send one or more channel/group IDs separated by commas.\n\n"
                "<i>Example:</i> <code>-1001234567890, -1009876543210</code>\n\n"
                "⚠️ Make sure the bot is an <b>admin with all rights</b> in all channels/groups.",
                reply_markup=_kb_cancel(_DUMP),
            )

        # ── Upload channel sub-menu ───────────────────────────────────────
        elif data == _UPLOAD:
            _AWAITING.pop(uid, None)
            await cq.edit_message_text(
                _text_upload(s),
                reply_markup=_kb_channel("upload", bool(s.get("upload_channels"))),
            )

        elif data == _UPLOAD_DEL:
            await db.update_setting(uid, "upload_channels", [])
            s["upload_channels"] = []
            await cq.edit_message_text(
                _text_upload(s),
                reply_markup=_kb_channel("upload", False),
            )
            await cq.answer("Upload channel(s) removed.")

        elif data == _UPLOAD_SET:
            _AWAITING[uid] = _UPLOAD_SET
            await cq.edit_message_text(
                "📤 <b>Set Upload Channel(s)</b>\n\n"
                "Send one or more channel/group IDs separated by commas.\n\n"
                "<i>Example:</i> <code>-1001234567890, -1009876543210</code>\n\n"
                "⚠️ Make sure the bot is an <b>admin with all rights</b> in all channels/groups.",
                reply_markup=_kb_cancel(_UPLOAD),
            )

        # ── Rename template sub-menu ──────────────────────────────────────
        elif data == _RENAME:
            _AWAITING.pop(uid, None)
            await cq.edit_message_text(
                _text_rename(s),
                reply_markup=_kb_rename(bool(s.get("rename_template"))),
            )

        elif data == _RENAME_DEL:
            await db.update_setting(uid, "rename_template", None)
            s["rename_template"] = None
            await cq.edit_message_text(
                _text_rename(s),
                reply_markup=_kb_rename(False),
            )
            await cq.answer("Template reset to default.")

        elif data == _RENAME_SET:
            _AWAITING[uid] = _RENAME_SET
            await cq.edit_message_text(
                "✏️ <b>Set Rename Template</b>\n\n"
                "Send your template string.  Example:\n"
                "<code>{title} - S{season:02d}E{episode:02d}.mkv</code>\n\n"
                "Must contain <code>{title}</code> and <code>{episode}</code>.\n"
                "<code>{season}</code> is optional.",
                reply_markup=_kb_cancel(_RENAME),
            )

        # ── Custom regex sub-menu ─────────────────────────────────────────
        elif data == _REGEX:
            _AWAITING.pop(uid, None)
            patterns = s.get("custom_patterns", [])
            await cq.edit_message_text(
                _text_regex(patterns),
                reply_markup=_kb_regex(patterns),
            )

        elif data == _REGEX_ADD:
            _AWAITING[uid] = _REGEX_ADD
            await cq.edit_message_text(
                "🔣 <b>Add Custom Regex Pattern</b>\n\n"
                "Send your pattern string.\n\n"
                "<b>Episode only (1 group):</b>\n"
                "  <code>[Ee]p?(\\d{1,4})</code>\n\n"
                "<b>Season + Episode (2 groups), pipe-separated spec:</b>\n"
                "  <code>[Ss](\\d{1,2})[Ee](\\d{1,3})|season,episode</code>\n\n"
                "The pattern is tested against the filename before being saved.",
                reply_markup=_kb_cancel(_REGEX),
            )

        elif data.startswith(_REGEX_DEL):
            idx_str = data[len(_REGEX_DEL):]
            try:
                idx = int(idx_str)
                patterns = s.get("custom_patterns", [])
                if 0 <= idx < len(patterns):
                    removed = patterns[idx]
                    await db.remove_custom_pattern(uid, removed)
                    patterns.pop(idx)
                    await cq.edit_message_text(
                        _text_regex(patterns),
                        reply_markup=_kb_regex(patterns),
                    )
                    await cq.answer("Pattern removed.")
                else:
                    await cq.answer("Pattern not found.")
            except (ValueError, IndexError):
                await cq.answer("Error removing pattern.")

        # ── Cancel → go back to where we came from ────────────────────────
        elif data == _CANCEL:
            _AWAITING.pop(uid, None)
            await cq.edit_message_text(_text_main(s, cq.from_user.mention), reply_markup=_kb_main(s))

        else:
            await cq.answer()

    # ── Message listener for awaited inputs ───────────────────────────────
    @app.on_message(filters.text & filters.private)
    @auth_only
    async def settings_input(client: Client, message: Message):
        uid   = message.from_user.id
        state = _AWAITING.get(uid)
        if state is None:
            return  # not waiting for input from this user

        db    = client.db
        text  = message.text.strip()

        # ── Channel IDs input (dump or upload) ────────────────────────────
        if state in (_DUMP_SET, _UPLOAD_SET):
            raw_ids = [x.strip() for x in text.replace(" ", "").split(",")]
            ids = []
            bad = []
            for r in raw_ids:
                try:
                    ids.append(int(r))
                except ValueError:
                    bad.append(r)

            if bad:
                await message.reply_text(
                    f"❌ Invalid ID(s): <code>{', '.join(bad)}</code>\n"
                    f"IDs must be integers, e.g. <code>-1001234567890</code>",
                    quote=True,
                )
                return

            key = "dump_channels" if state == _DUMP_SET else "upload_channels"
            await db.update_setting(uid, key, ids)
            _AWAITING.pop(uid, None)

            kind = "Dump" if state == _DUMP_SET else "Upload"
            await message.reply_text(
                f"✅ <b>{kind} channel(s) saved:</b> <code>{', '.join(str(i) for i in ids)}</code>\n\n"
                f"<i>Make sure the bot is admin in all of them!</i>",
                quote=True,
            )

        # ── Rename template input ─────────────────────────────────────────
        elif state == _RENAME_SET:
            if "{title}" not in text:
                await message.reply_text(
                    "❌ Template must contain <code>{title}</code>.", quote=True
                )
                return
            if "{episode" not in text:
                await message.reply_text(
                    "❌ Template must contain <code>{episode}</code> (or <code>{episode:02d}</code>).",
                    quote=True,
                )
                return
            # Try formatting with dummy values to catch bad format strings
            try:
                text.format(title="Test", season=1, episode=1)
            except (KeyError, ValueError) as exc:
                await message.reply_text(
                    f"❌ Invalid template: <code>{exc}</code>", quote=True
                )
                return

            await db.update_setting(uid, "rename_template", text)
            _AWAITING.pop(uid, None)
            await message.reply_text(
                f"✅ <b>Rename template saved:</b>\n<code>{text}</code>",
                quote=True,
            )

        # ── Custom regex input ────────────────────────────────────────────
        elif state == _REGEX_ADD:
            # Parse the pipe-separated format first
            if "|" in text:
                pattern_str, spec = text.split("|", 1)
                group_names = [g.strip() for g in spec.split(",")]
                for g in group_names:
                    if g not in ("season", "episode"):
                        await message.reply_text(
                            f"❌ Group name must be <code>season</code> or <code>episode</code>, "
                            f"got: <code>{g}</code>",
                            quote=True,
                        )
                        return
            else:
                pattern_str = text
                group_names = ["episode"]

            # Validate the regex compiles
            try:
                compiled = re.compile(pattern_str)
            except re.error as exc:
                await message.reply_text(
                    f"❌ Invalid regex: <code>{exc}</code>", quote=True
                )
                return

            # Check group count matches spec
            if compiled.groups != len(group_names):
                await message.reply_text(
                    f"❌ Pattern has <b>{compiled.groups}</b> capture group(s) "
                    f"but spec has <b>{len(group_names)}</b> name(s).",
                    quote=True,
                )
                return

            await db.add_custom_pattern(uid, text)
            _AWAITING.pop(uid, None)
            await message.reply_text(
                f"✅ <b>Pattern added:</b>\n<code>{text}</code>",
                quote=True,
            )
