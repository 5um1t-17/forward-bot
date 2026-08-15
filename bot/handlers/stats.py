"""Statistics."""
from __future__ import annotations

from telethon import events

from bot import keyboards, text
from bot.db import db
from bot.handlers.common import answer, edit, is_admin


async def handle(bot, event: events.CallbackQuery.Event, data: str) -> bool:
    if data == "stats" or data.startswith("stats:"):
        return await _route(bot, event, data)
    return False


async def _route(bot, event, data: str) -> bool:
    uid = event.sender_id
    if data == "stats":
        return await _show(bot, event, uid)
    if data == "stats:global":
        if not is_admin(uid):
            await answer(event, "Admins only", alert=True)
            return True
        return await _show_global(bot, event, uid)
    return False


async def _show(bot, event, uid: int) -> bool:
    user = await db.get_user(uid)
    settings = await db.get_settings(uid)
    per_mode = await db.transferred_by_mode(uid)
    total = await db.transferred_count(uid)
    today = await db.transferred_today(uid)
    recent = await db.user_logs(uid, limit=3)
    await edit(
        event,
        text.stats_text(user, settings, per_mode, total, today, recent),
        keyboards.stats_keyboard(is_admin(uid)),
    )
    return True


async def _show_global(bot, event, uid: int) -> bool:
    total_users = await db.users.count_documents({})
    total_accounts = await db.sessions.count_documents({})
    per_account = await db.stats_by_user()
    sessions = await db.get_all_sessions()
    total_msgs = 0
    for entry in per_account:
        total_msgs += entry["count"]
    await edit(
        event,
        text.admin_stats(total_users, total_accounts, total_msgs, per_account, sessions),
        keyboards.stats_keyboard(True),
    )
    return True
