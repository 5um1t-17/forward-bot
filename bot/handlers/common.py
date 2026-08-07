"""Shared helpers for handlers."""
from __future__ import annotations

import asyncio
import logging
import time

from telethon import events
from telethon.errors import FloodWaitError, MessageNotModifiedError

from bot.config import config

log = logging.getLogger("bot.handlers")

# Global "do not hit the bot API" window. Telegram rate-limits the bot account
# (FloodWaitError) when it sends/edits too many messages in a short window
# (e.g. a fast transfer updating the progress message every message). While a
# flood wait is in force, every bot message operation would just fail again and
# burn more quota, so we hold off until the window passes.
_flood_until = 0.0
_flood_lock = asyncio.Lock()

# Monotonic timestamp of the last *successful* bot RPC (an edit, an answer, a
# send). The bot health watchdog uses this to avoid pinging the connection
# while it is demonstrably doing real work (e.g. a transfer editing its
# progress message every few seconds): under that load the ping RPC can time
# out even though the link is perfectly alive, and a forced reconnect would
# cancel every running transfer for no reason.
_last_bot_activity = 0.0


def note_bot_activity() -> None:
    """Record that the bot connection just answered a real request."""
    global _last_bot_activity
    _last_bot_activity = time.monotonic()


def bot_idle_seconds() -> float:
    """Seconds since the last successful bot RPC (``inf`` if none yet)."""
    return time.monotonic() - _last_bot_activity


async def note_flood(seconds: int | float | None) -> None:
    """Record that Telegram demanded a flood wait of ``seconds``."""
    if not seconds:
        return
    global _flood_until
    async with _flood_lock:
        _flood_until = max(_flood_until, time.monotonic() + float(seconds))


def flood_remaining() -> float:
    return max(0.0, _flood_until - time.monotonic())


def flood_blocked() -> bool:
    return time.monotonic() < _flood_until


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


async def answer(event: events.CallbackQuery.Event, text: str | None = None, alert: bool = False) -> None:
    try:
        await event.answer(text, alert=alert)
        note_bot_activity()
    except FloodWaitError as exc:
        log.warning("answer flood wait: %ss", exc.seconds)
        await note_flood(exc.seconds)
        note_bot_activity()  # the link responded, even if rate-limited
    except Exception:
        log.debug("answer failed", exc_info=True)


async def edit(event: events.CallbackQuery.Event, text: str, kb=None, *, timeout: float = 20.0) -> bool:
    """Edit the callback message in place.

    Returns ``True`` if the message was updated (or already had this content),
    ``False`` if the edit could not be performed (timed out, message deleted,
    network error, ...). Callers that must show new content even when the
    original message is gone can use the return value to fall back to sending
    a fresh message instead of leaving the user stuck on stale UI.
    """
    if flood_blocked():
        log.info("edit skipped: bot flood-limited (%ds remaining)", int(flood_remaining()))
        return False
    try:
        await asyncio.wait_for(
            event.edit(text, buttons=kb, parse_mode="html"),
            timeout=timeout,
        )
        note_bot_activity()
        return True
    except asyncio.TimeoutError:
        log.debug("edit timed out for uid=%s", getattr(event, "sender_id", None))
    except MessageNotModifiedError:
        note_bot_activity()
        return True  # already shows the requested content
    except FloodWaitError as exc:
        log.warning("edit flood wait: %ss", exc.seconds)
        await note_flood(exc.seconds)
        note_bot_activity()  # the link responded, even if rate-limited
    except Exception:
        log.warning("edit failed", exc_info=True)
    return False


async def safe_send(bot, uid: int, text: str, kb=None, *, parse_mode: str = "html"):
    """Send a fresh bot message, respecting the flood guard.

    Returns the sent message object on success, or ``False`` if the message
    could not be sent (flood-limited, network error, ...). While Telegram is
    rate-limiting the bot account this returns ``False`` without attempting
    the request, so a flood wait is never made worse by hammering the API.
    """
    if flood_blocked():
        log.info("send skipped for uid=%s: bot flood-limited (%ds remaining)", uid, int(flood_remaining()))
        return False
    try:
        sent = await bot.send_message(uid, text, buttons=kb, parse_mode=parse_mode)
        note_bot_activity()
        return sent
    except FloodWaitError as exc:
        log.warning("send flood wait: %ss for uid=%s", exc.seconds, uid)
        await note_flood(exc.seconds)
        note_bot_activity()  # the link responded, even if rate-limited
        return False
    except Exception:
        log.warning("send failed for uid=%s", uid, exc_info=True)
        return False
