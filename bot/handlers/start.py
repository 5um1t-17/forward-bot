"""Main menu + /start."""
from __future__ import annotations

from telethon import events

from bot import keyboards, text
from bot.db import db


async def cmd_start(bot, event: events.NewMessage.Event) -> None:
    uid = event.sender_id
    sender = event.sender
    await db.upsert_user(
        uid,
        getattr(sender, "first_name", "") or "",
        getattr(sender, "username", "") or "",
    )
    user = await db.get_user(uid)
    await bot.send_message(uid, text.menu_text(user), buttons=keyboards.main_menu(), parse_mode="html")
