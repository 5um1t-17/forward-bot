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


def build_id() -> str:
    """Short identifier of the running code for log correlation.

    Falls back to the Git ref/commit when inside a checkout, otherwise to a
    build marker. Render deployments build from the pushed commit, so this lets
    us confirm the running instance actually contains the destination fixes.
    """
    try:
        import subprocess

        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:  # noqa: BLE001 - best effort
        pass
    return "unknown-build"


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
            try:
                if await accounts.handle_pending(bot, event, pending):
                    return
                if await transfer.handle_pending(bot, event, pending):
                    return
            except Exception:
                log.exception("pending handler error for uid=%s kind=%s", uid, pending)
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
        log.info("callback uid=%s data=%s", uid, data)
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
    """Lightweight RPC round-trip used by the health watchdog.

    ``FloodWaitError`` counts as *alive*: the MTProto link responded, the bot is
    merely rate-limited, and forcing a reconnect in that state would only cancel
    running transfers for no reason.
    """
    from telethon.errors import FloodWaitError

    try:
        await asyncio.wait_for(
            bot(GetNearestDcRequest()), timeout=timeout or config.BOT_PING_TIMEOUT
        )
        return True
    except FloodWaitError:
        return True
    except asyncio.CancelledError:
        raise
    except (asyncio.TimeoutError, TimeoutError, OSError, ConnectionError) as exc:
        log.warning("bot health ping failed: %s: %s", type(exc).__name__, exc)
        return False
    except Exception as exc:  # noqa: BLE001 - any other error means the link is dead
        log.warning("bot health ping failed: %s: %s", type(exc).__name__, exc)
        return False


async def _bot_health_watchdog(bot: TelegramClient) -> None:
    """Ping the bot every interval; force a reconnect only after repeated failures.

    ``run_until_disconnected`` only returns on a clean disconnect — if the
    MTProto link wedges without raising, the process would sit frozen forever.
    This watchdog detects that and breaks the loop so :func:`main` reconnects.

    False-positive protections:

    * The check is purely monotonic-time based; no wall-clock timestamps and no
      state carried over from a previous bot instance, so a fresh connection can
      never be treated as "stale" (the old code had the same class of bug as the
      transfer watchdog).
    * A single transient failure is tolerated; we require ``required_failures``
      (3) consecutive failures spanning ~90s before forcing a reconnect, so a
      busy event loop or a sporadic slow ping does not kill the bot.
    * While the bot account is flood-limited, pings fail spuriously even though
      the link is perfectly alive, so those intervals are skipped entirely —
      otherwise a transfer's progress edits would trigger a disconnect cascade.
    * While the bot has answered any real RPC recently (progress edits happen
      every few seconds during a transfer), the ping is skipped outright: a
      busy, responding bot is by definition alive, and the ping RPC is the
      least reliable signal under that load. This is the fix for the periodic
      "3 consecutive failures, forcing reconnect" every ~4 minutes during
      transfers, which was canceling every running job.
    """
    from bot.handlers.common import bot_idle_seconds, flood_blocked

    consecutive_failures = 0
    required_failures = 3
    while True:
        await asyncio.sleep(config.BOT_WATCHDOG_INTERVAL)
        if bot_idle_seconds() < config.BOT_ACTIVITY_SKIP:
            # The bot has demonstrably done real work recently — not idle, not
            # wedged. Reset the streak and keep listening.
            consecutive_failures = 0
            continue
        if flood_blocked():
            consecutive_failures = 0
            continue
        if not bot.is_connected():
            consecutive_failures += 1
        elif await _bot_alive(bot):
            consecutive_failures = 0
            continue
        else:
            consecutive_failures += 1
        if consecutive_failures >= required_failures:
            log.warning(
                "bot health watchdog: %d consecutive failures, forcing reconnect",
                consecutive_failures,
            )
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
    log.info("build=%s", build_id())
    log.info("FETCH_DIALOGS_TIMEOUT=%s CLIENT_CONNECT_TIMEOUT=%s", config.FETCH_DIALOGS_TIMEOUT, config.CLIENT_CONNECT_TIMEOUT)

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
