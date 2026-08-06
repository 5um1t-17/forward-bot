"""Shared helpers for handlers."""
from __future__ import annotations

import asyncio
import logging

from telethon import events
from telethon.errors import MessageNotModifiedError

from bot.config import config

log = logging.getLogger("bot.handlers")


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


async def answer(event: events.CallbackQuery.Event, text: str | None = None, alert: bool = False) -> None:
    try:
        await event.answer(text, alert=alert)
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
    try:
        await asyncio.wait_for(
            event.edit(text, buttons=kb, parse_mode="html"),
            timeout=timeout,
        )
        return True
    except asyncio.TimeoutError:
        log.debug("edit timed out for uid=%s", getattr(event, "sender_id", None))
    except MessageNotModifiedError:
        return True  # already shows the requested content
    except Exception:
        log.warning("edit failed", exc_info=True)
    return False
