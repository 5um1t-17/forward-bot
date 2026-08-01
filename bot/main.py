"""Telegram Message Transfer Bot — entry point."""
from __future__ import annotations

import asyncio
import logging
import os

from telethon import TelegramClient, events

from bot import keyboards, text
from bot.config import config
from bot.db import db
from bot.handlers import accounts, jobs, settings as settings_handler, start, stats, transfer
from bot.logger import setup_logging
from bot.scheduler import Scheduler
from bot.state import store

log = logging.getLogger("bot.main")


def register_handlers(bot: TelegramClient) -> None:
    @bot.on(events.NewMessage(incoming=True))
    async def on_message(event: events.NewMessage.Event) -> None:
        if not event.is_private:
            return
        uid = event.sender_id
        if uid is None:
            return
        await db.upsert_user(
            uid,
            getattr(event.sender, "first_name", "") or "",
            getattr(event.sender, "username", "") or "",
        )
        raw = event.raw_text.strip() if event.raw_text else ""

        pending = store.pending(uid)
        if pending:
            if await accounts.handle_pending(bot, event, pending):
                return
            if await transfer.handle_pending(bot, event, pending):
                return
            store.set_pending(uid, None)

        low = raw.lower()
        if low.startswith("/start"):
            await start.cmd_start(bot, event)
        elif low in ("/cancel", "/stop", "cancel"):
            await _cancel(bot, event, uid)
        elif event.message.forward is not None:
            # allow forwarded-message resolution even without a pending step
            await transfer.handle_pending(bot, event, "tr_source")
        elif raw:
            user = await db.get_user(uid)
            await bot.send_message(uid, text.menu_text(user), buttons=keyboards.main_menu(), parse_mode="html")
        else:
            await bot.send_message(uid, text.menu_text(await db.get_user(uid)), buttons=keyboards.main_menu(), parse_mode="html")

    @bot.on(events.CallbackQuery())
    async def on_callback(event: events.CallbackQuery.Event) -> None:
        uid = event.sender_id
        if uid is None:
            return
        data = event.data.decode()
        if data == "menu":
            store.set_pending(uid, None)
            store.reset_transfer(uid)
            store.login.pop(uid, None)
            user = await db.get_user(uid)
            await event.edit(text.menu_text(user), buttons=keyboards.main_menu(), parse_mode="html")
            return
        for handler in (accounts.handle, transfer.handle, jobs.handle, settings_handler.handle, stats.handle):
            try:
                if await handler(bot, event, data):
                    return
            except Exception:
                log.exception("callback handler error for %s", data)
        try:
            await event.answer()
        except Exception:
            pass


async def _cancel(bot, event, uid: int) -> None:
    store.set_pending(uid, None)
    store.reset_transfer(uid)
    store.login.pop(uid, None)
    running = store.running.pop(uid, None)
    if running is not None:
        running.request_stop()
    user = await db.get_user(uid)
    await bot.send_message(
        uid,
        "❌ Cancelled. Nothing is running.\n\n" + text.menu_text(user),
        buttons=keyboards.main_menu(),
        parse_mode="html",
    )


async def main() -> None:
    setup_logging()
    if not config.configured:
        log.error(
            "Configuration incomplete. Copy .env.example to .env and set "
            "API_ID, API_HASH and BOT_TOKEN."
        )
        return

    await db.init()
    log.info("Connected to MongoDB at %s/%s", config.MONGO_URI, config.MONGO_DB)

    os.makedirs(config.SESSION_DIR, exist_ok=True)
    bot = TelegramClient(os.path.join(config.SESSION_DIR, "bot"), config.API_ID, config.API_HASH)
    await bot.start(bot_token=config.BOT_TOKEN)
    me = await bot.get_me()
    log.info("Bot running as @%s", me.username)

    register_handlers(bot)

    scheduler = Scheduler(bot)
    scheduler_task = asyncio.create_task(scheduler.loop())

    try:
        await bot.run_until_disconnected()
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        await bot.disconnect()
        if db.client is not None:
            db.client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
