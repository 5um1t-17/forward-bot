"""Settings menu."""
from __future__ import annotations

from telethon import events

from bot import keyboards, text
from bot.db import db
from bot.handlers.common import answer, edit


async def handle(bot, event: events.CallbackQuery.Event, data: str) -> bool:
    if data == "set" or data.startswith("set:"):
        return await _route(bot, event, data)
    return False


async def _route(bot, event, data: str) -> bool:
    uid = event.sender_id
    if data == "set":
        return await _show(bot, event, uid)
    if data.startswith("set:delay:"):
        return await _set(bot, event, uid, "forward_delay", float(data.split(":", 2)[2]))
    if data.startswith("set:threads:"):
        return await _set(bot, event, uid, "threads", int(data.split(":", 2)[2]))
    if data.startswith("set:retry:"):
        return await _set(bot, event, uid, "retry_count", int(data.split(":", 2)[2]))
    if data.startswith("set:flood:"):
        return await _set(bot, event, uid, "handle_flood", data.split(":", 2)[2] == "on")
    if data.startswith("set:resume:"):
        return await _set(bot, event, uid, "auto_resume", data.split(":", 2)[2] == "on")
    if data.startswith("set:notif:"):
        return await _set(bot, event, uid, "notifications", data.split(":", 2)[2] == "on")
    if data.startswith("set:theme:"):
        return await _set(bot, event, uid, "dark_theme", data.split(":", 2)[2] == "on")
    if data == "set:delay":
        return await _choice(bot, event, "delay", [("0", "0s (none)"), ("0.5", "0.5s"), ("1", "1s"), ("2", "2s")])
    if data == "set:threads":
        return await _choice(bot, event, "threads", [("1", "1 thread"), ("2", "2 threads"), ("3", "3 threads"), ("4", "4 threads"), ("5", "5 threads"), ("8", "8 threads"), ("10", "10 threads")])
    if data == "set:retry":
        return await _choice(bot, event, "retry", [("3", "3 times"), ("5", "5 times"), ("0", "Unlimited")])
    if data == "set:flood":
        return await _toggle(bot, event, uid, "handle_flood")
    if data == "set:resume":
        return await _toggle(bot, event, uid, "auto_resume")
    if data == "set:notif":
        return await _toggle(bot, event, uid, "notifications")
    if data == "set:theme":
        return await _toggle(bot, event, uid, "dark_theme")
    return False


async def _show(bot, event, uid: int) -> bool:
    s = await db.get_settings(uid)
    await edit(event, text.settings_menu(s), keyboards.settings_keyboard(s))
    return True


async def _choice(bot, event, key: str, items) -> bool:
    await edit(event, f"⚙️ Choose <b>{key.replace('_', ' ')}</b>:", keyboards.settings_choice_keyboard(key, items))
    return True


async def _toggle(bot, event, uid: int, key: str) -> bool:
    s = await db.get_settings(uid)
    await db.set_setting(uid, key, not s.get(key))
    await answer(event, "Saved")
    await _show(bot, event, uid)
    return True


async def _set(bot, event, uid: int, key: str, value) -> bool:
    await db.set_setting(uid, key, value)
    await answer(event, "Saved")
    await _show(bot, event, uid)
    return True
