"""Text-command shortcuts, exposed in the Telegram menu next to the input box."""
from __future__ import annotations

import logging

from bot import keyboards, text
from bot.db import db
from bot.handlers.common import is_admin
from bot.state import store

log = logging.getLogger("bot.commands")


async def cmd_transfer(bot, event, uid: int) -> bool:
    sid = await db.get_active_sid(uid)
    if not sid:
        await event.respond(text.err_no_account())
        return True
    store.reset_transfer(uid)
    wiz = store.get_transfer(uid)
    wiz.step = "source_type"
    await event.respond(
        "📥 <b>Transfer Messages</b>\n\n"
        "What kind of source chat is it?\n"
        "(This is used for validation only — auto-detect works too.)",
        buttons=keyboards.source_type_keyboard(),
        parse_mode="html",
    )
    return True


async def cmd_accounts(bot, event, uid: int) -> bool:
    accounts = await db.get_user_sessions(uid)
    active = await db.get_active_sid(uid)
    await event.respond(
        text.accounts_menu(accounts, active),
        buttons=keyboards.accounts_menu_keyboard(accounts, active),
        parse_mode="html",
    )
    return True


async def cmd_jobs(bot, event, uid: int) -> bool:
    jobs = await db.user_jobs(uid)
    await event.respond(
        text.jobs_menu(jobs),
        buttons=keyboards.jobs_menu_keyboard(jobs),
        parse_mode="html",
    )
    return True


async def cmd_settings(bot, event, uid: int) -> bool:
    settings = await db.get_settings(uid)
    await event.respond(
        text.settings_menu(settings),
        buttons=keyboards.settings_keyboard(settings),
        parse_mode="html",
    )
    return True


async def cmd_stats(bot, event, uid: int) -> bool:
    user = await db.get_user(uid)
    settings = await db.get_settings(uid)
    per_mode = await db.transferred_by_mode(uid)
    total = await db.transferred_count(uid)
    today = await db.transferred_today(uid)
    recent = await db.user_logs(uid, limit=3)
    await event.respond(
        text.stats_text(user, settings, per_mode, total, today, recent),
        buttons=keyboards.stats_keyboard(is_admin(uid)),
        parse_mode="html",
    )
    return True


async def cmd_cleanup(bot, event, uid: int) -> bool:
    raw = (event.raw_text or "").strip().lower()
    if "yes" in raw or "confirm" in raw:
        store.set_pending(uid, None)
        deleted = await db.clear_transferred(uid)
        await event.respond(
            f"🧹 <b>Cleanup complete</b>\n\n"
            f"Deleted <b>{deleted}</b> dedup record(s).\n"
            "Messages will be transferred again on the next run.",
            parse_mode="html",
        )
        return True
    count = await db.transferred_count(uid)
    await event.respond(
        "🧹 <b>Cleanup dedup records</b>\n\n"
        "The database keeps one record per transferred message so re-runs skip them.\n\n"
        f"You currently have <b>{count}</b> record(s) for your accounts.\n\n"
        "Type <code>/cleanup yes</code> to delete them all so every message is copied again.",
        parse_mode="html",
    )
    return True
