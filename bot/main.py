"""Telegram Message Transfer Bot — entry point."""
from __future__ import annotations

import asyncio
import logging
import os

from telethon import TelegramClient, events
from telethon.tl.functions.help import GetNearestDcRequest

from bot import keyboards, text
from bot.client_pool import client_pool
from bot.config import config
from bot.db import db
from bot.health import mark_bot_alive
from bot.handlers import accounts, commands, jobs, settings as settings_handler, start, stats, transfer
from bot.logger import setup_logging
from bot.scheduler import Scheduler
from bot.state import store

log = logging.getLogger("bot.main")


_COMMANDS = {
    "/transfer": commands.cmd_transfer,
    "/accounts": commands.cmd_accounts,
    "/jobs": commands.cmd_jobs,
    "/settings": commands.cmd_settings,
    "/stats": commands.cmd_stats,
    "/cleanup": commands.cmd_cleanup,
    "/help": None,  # handled inline
}


async def register_bot_commands(bot: TelegramClient) -> None:
    """Expose commands in the Telegram menu button next to the input box."""
    from telethon.tl import functions, types

    entries = [
        types.BotCommand("start", "Main menu"),
        types.BotCommand("transfer", "Transfer messages"),
        types.BotCommand("accounts", "Manage accounts"),
        types.BotCommand("jobs", "Saved jobs"),
        types.BotCommand("settings", "Settings"),
        types.BotCommand("stats", "Statistics"),
        types.BotCommand("cleanup", "Reset dedup records"),
        types.BotCommand("cancel", "Abort current step"),
    ]
    try:
        await bot(
            functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(), lang_code="", commands=entries
            )
        )
        log.info("Registered %d bot commands", len(entries))
    except Exception:  # noqa: BLE001
        log.warning("Failed to register bot commands", exc_info=True)


def register_handlers(bot: TelegramClient) -> None:
    @bot.on(events.NewMessage(incoming=True))
    async def on_message(event: events.NewMessage.Event) -> None:
        if not event.is_private:
            return
        uid = event.sender_id
        if uid is None:
            return
        try:
            await db.upsert_user(
                uid,
                getattr(event.sender, "first_name", "") or "",
                getattr(event.sender, "username", "") or "",
            )
        except Exception:
            log.debug("upsert_user failed", exc_info=True)
        raw = event.raw_text.strip() if event.raw_text else ""
        low = raw.lower()
        token = low.split(" ", 1)[0] if low else ""

        if token in ("/cancel", "/stop") or low == "cancel":
            await _cancel(bot, event, uid)
            return
        if token == "/skip":
            await transfer.cmd_skip(bot, event, uid)
            return
        if token == "/start":
            await start.cmd_start(bot, event)
            return
        if token == "/help":
            await event.respond(text.commands_help(), parse_mode="html")
            return
        if token in _COMMANDS:
            await _COMMANDS[token](bot, event, uid)
            return

        pending = store.pending(uid)
        if pending:
            if await accounts.handle_pending(bot, event, pending):
                return
            if await transfer.handle_pending(bot, event, pending):
                return
            store.set_pending(uid, None)

        try:
            if event.message.forward is not None:
                await transfer.handle_pending(bot, event, "tr_source")
            elif raw:
                user = await db.get_user(uid)
                await bot.send_message(uid, text.menu_text(user), buttons=keyboards.main_menu(), parse_mode="html")
            else:
                await bot.send_message(uid, text.menu_text(await db.get_user(uid)), buttons=keyboards.main_menu(), parse_mode="html")
        except Exception:
            log.exception("message handler fallback error for uid=%s", uid)

    @bot.on(events.CallbackQuery())
    async def on_callback(event: events.CallbackQuery.Event) -> None:
        uid = event.sender_id
        if uid is None:
            return
        if not event.data:
            try:
                await event.answer()
            except Exception:
                pass
            return
        try:
            data = event.data.decode()
        except Exception:
            log.debug("callback decode failed", exc_info=True)
            try:
                await event.answer()
            except Exception:
                pass
            return
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
    store.progress.pop(uid, None)
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


async def _bot_alive(bot: TelegramClient, timeout: float | None = None) -> bool:
    """Lightweight RPC round-trip used by the health watchdog."""
    try:
        await asyncio.wait_for(
            bot(GetNearestDcRequest()), timeout=timeout or config.BOT_PING_TIMEOUT
        )
        return True
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        return False


async def _bot_health_watchdog(bot: TelegramClient) -> None:
    """Ping the bot every interval; force a reconnect when it goes silent.

    ``run_until_disconnected`` only returns on a clean disconnect — if the
    MTProto link wedges without raising, the process would sit frozen forever.
    This watchdog detects that and breaks the loop so :func:`main` reconnects.
    """
    while True:
        await asyncio.sleep(config.BOT_WATCHDOG_INTERVAL)
        if not await _bot_alive(bot):
            log.warning("bot health watchdog: bot unresponsive, forcing reconnect")
            try:
                await bot.disconnect()
            except Exception:
                pass
            return


async def _pool_sweep_loop() -> None:
    """Periodically drop half-dead account clients from the pool."""
    while True:
        await asyncio.sleep(config.POOL_SWEEP_INTERVAL)
        try:
            await client_pool.sweep()
        except Exception:
            log.exception("client pool sweep failed")


async def run_bot_once() -> None:
    """Connect, register handlers, and run until disconnected or unhealthy."""
    bot = TelegramClient(
        os.path.join(config.SESSION_DIR, "bot"), config.API_ID, config.API_HASH
    )
    await bot.start(bot_token=config.BOT_TOKEN)
    me = await bot.get_me()
    log.info("Bot running as @%s", me.username)

    await register_bot_commands(bot)

    register_handlers(bot)

    scheduler = Scheduler(bot)
    scheduler_task = asyncio.create_task(scheduler.loop())
    sweep_task = asyncio.create_task(_pool_sweep_loop())
    watchdog_task = asyncio.create_task(_bot_health_watchdog(bot))

    # Bot is ready. The flag intentionally stays True across the reconnect
    # backoff so the health endpoint never reports 503 during a brief
    # disconnect (which would make the platform restart the process
    # mid-transfer). Only a permanently dead thread reports 503.
    mark_bot_alive(True)
    try:
        await bot.run_until_disconnected()
    finally:
        for task in (watchdog_task, sweep_task, scheduler_task):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await bot.disconnect()


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

    from bot.transfer_engine import TransferEngine

    TransferEngine.cleanup_stale_temp()

    os.makedirs(config.SESSION_DIR, exist_ok=True)

    # Reconnect-on-disconnect loop with backoff: an unexpected disconnect
    # (network blip, Telegram-side drop, watchdog trigger) self-heals without
    # a manual Render redeploy.
    backoff = config.RECONNECT_DELAY
    while True:
        try:
            await run_bot_once()
        except asyncio.CancelledError:
            mark_bot_alive(False)
            raise
        except Exception:
            log.exception("bot loop crashed; restarting")
        store.cancel_all_running()
        log.warning("Bot disconnected; reconnecting in %.0fs", backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
