"""Shared helpers for handlers."""
from __future__ import annotations

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


async def edit(event: events.CallbackQuery.Event, text: str, kb=None) -> None:
    try:
        await event.edit(text, buttons=kb, parse_mode="html")
    except MessageNotModifiedError:
        pass
    except Exception:
        log.warning("edit failed", exc_info=True)
