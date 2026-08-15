"""Account management: add (login), switch active, delete."""
from __future__ import annotations

import asyncio
import logging

from telethon import Button, events
from telethon.errors import (
    FloodWaitError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    SessionPasswordNeededError,
    PhoneNumberInvalidError,
)

from bot import keyboards, text
from bot.config import config
from bot.db import db
from bot.handlers.common import answer, edit
from bot.session_manager import session_manager
from bot.state import LoginState, store

log = logging.getLogger("bot.accounts")

_ACTIONS = {"acct", "acct:add", "acct:sel", "acct:del", "acct:del2"}


async def handle(bot, event: events.CallbackQuery.Event, data: str) -> bool:
    if data == "acct" or data.startswith("acct:"):
        return await _route(bot, event, data)
    return False


async def _route(bot, event, data: str) -> bool:
    if data == "acct":
        return await _show(bot, event)
    if data == "acct:add":
        return await _start_login(bot, event)
    if data.startswith("acct:sel:"):
        return await _switch(bot, event, data.split(":", 2)[2])
    if data == "acct:del":
        return await _show_delete(bot, event)
    if data.startswith("acct:del:"):
        return await _confirm_delete(bot, event, data.split(":", 2)[2])
    if data.startswith("acct:del2:"):
        return await _do_delete(bot, event, data.split(":", 2)[2])
    return False


async def _show(bot, event) -> bool:
    uid = event.sender_id
    accounts = await db.get_user_sessions(uid)
    active = await db.get_active_sid(uid)
    await edit(event, text.accounts_menu(accounts, active), keyboards.accounts_menu_keyboard(accounts, active))
    return True


async def _start_login(bot, event) -> bool:
    uid = event.sender_id
    store.login[uid] = LoginState(step="phone")
    store.set_pending(uid, "login_phone")
    await edit(event, text.add_account_step1(), keyboards.back_row())
    return True


async def _switch(bot, event, sid: str) -> bool:
    uid = event.sender_id
    if not await db.get_session(uid, sid):
        await answer(event, "Account not found")
        return True
    await db.set_active_sid(uid, sid)
    await answer(event, "Active account switched")
    await _show(bot, event)
    return True


async def _show_delete(bot, event) -> bool:
    uid = event.sender_id
    accounts = await db.get_user_sessions(uid)
    if not accounts:
        await answer(event, "No accounts to delete")
        await _show(bot, event)
        return True
    await edit(event, "🗑 Select an account to delete:", keyboards.accounts_delete_keyboard(accounts))
    return True


async def _confirm_delete(bot, event, sid: str) -> bool:
    uid = event.sender_id
    acc = await db.get_session(uid, sid)
    if not acc:
        await answer(event, "Account not found")
        return True
    kb = [
        [Button.inline("🗑 Yes, delete", f"acct:del2:{sid}".encode())],
        [Button.inline("🔙 Cancel", b"acct")],
    ]
    await edit(event, f"Delete account <b>{acc['name']}</b> ({acc['phone']})?\n\nThis cannot be undone.", kb)
    return True


async def _do_delete(bot, event, sid: str) -> bool:
    uid = event.sender_id
    await db.delete_session(uid, sid)
    await client_cleanup(uid, sid)
    await answer(event, "Account deleted")
    await _show(bot, event)
    return True


async def client_cleanup(uid: int, sid: str) -> None:
    from bot.client_pool import client_pool

    await client_pool.dispose(uid, sid)


# ----------------------------------------------------------------------
# login flow text input
# ----------------------------------------------------------------------
async def handle_pending(bot, event: events.NewMessage.Event, kind: str) -> bool:
    uid = event.sender_id
    if kind == "login_phone":
        return await _on_phone(bot, event, uid)
    if kind == "login_code":
        return await _on_code(bot, event, uid)
    if kind == "login_password":
        return await _on_password(bot, event, uid)
    return False


async def _on_phone(bot, event, uid: int) -> bool:
    state = store.login.get(uid)
    phone = event.raw_text.strip()
    if not phone:
        return True
    try:
        client = session_manager.build_client()
        await asyncio.wait_for(client.connect(), timeout=config.CLIENT_CONNECT_TIMEOUT)
        result = await asyncio.wait_for(
            client.send_code_request(phone), timeout=config.OP_TIMEOUT
        )
        state = LoginState(phone=phone, code_hash=result.phone_code_hash, client=client, step="code")
        store.login[uid] = state
        store.set_pending(uid, "login_code")
        sent_type = type(result.type).__name__
        timeout = getattr(result, "next_code_timeout", 0) or 0
        registered = getattr(result, "phone_registered", None)
        log.info(
            "send_code_request ok: phone=%s via=%s registered=%s next_code_timeout=%ss",
            phone, sent_type, registered, timeout,
        )
        await event.respond(
            text.add_account_step2() + _code_delivery_hint(sent_type, timeout),
            parse_mode="html",
        )
    except PhoneNumberInvalidError:
        await event.respond("⚠️ That phone number is not valid. Try again.")
    except FloodWaitError as exc:
        log.warning("send_code_request flood: %s", exc)
        await event.respond(
            f"⚠️ Telegram is rate-limiting code requests.\n\n"
            f"Wait <b>{exc.seconds}</b> seconds before trying again — retrying resets the timer.",
            parse_mode="html",
        )
    except Exception as exc:
        log.warning("send_code_request failed: %s", exc)
        await event.respond(f"⚠️ Could not request the code: {str(exc)[:200]}\n\nTry again.")
    return True


def _code_delivery_hint(sent_type: str, timeout: int) -> str:
    hints = {
        "SentCodeTypeSms": "\n\n📱 Telegram says the code was sent <b>via SMS</b>.\nCheck your SMS (also spam/junk).",
        "SentCodeTypeApp": (
            "\n\n📲 Telegram sent the code <b>inside the Telegram app</b> of this number — "
            "an SMS will NOT be sent.\n"
            "Open the Telegram app on the device where <b>this number is logged in</b> and look for a "
            "\"Login code\" notification or card (it may say \"Code: XXXXX\").\n"
            "If you don't have access to a device where this number is logged in, you cannot add this account."
        ),
        "SentCodeTypeCall": "\n\n📞 Telegram says the code was sent <b>via a phone call</b>.\nAnswer the call and note the code.",
        "SentCodeTypeFlashCall": "\n\n📞 Telegram says the code was sent <b>via a flash call</b>.\nCheck missed calls for the last digits of the caller number.",
    }
    hint = hints.get(sent_type)
    if not hint:
        hint = "\n\n📨 Check your Telegram app and SMS for the login code."
    if timeout and timeout > 0:
        hint += (
            f"\n\n⏳ Wait at least <b>{timeout}</b> seconds before retrying this number — "
            f"each retry resets the timer and suppresses delivery."
        )
    return hint


async def _on_code(bot, event, uid: int) -> bool:
    state = store.login.get(uid)
    if not state or not state.client:
        store.set_pending(uid, None)
        return True
    code = event.raw_text.strip()
    try:
        await asyncio.wait_for(
            state.client.sign_in(phone=state.phone, code=code, phone_code_hash=state.code_hash),
            timeout=config.OP_TIMEOUT,
        )
    except SessionPasswordNeededError:
        state.step = "password"
        store.set_pending(uid, "login_password")
        await event.respond(text.add_account_step3())
        return True
    except PhoneCodeInvalidError:
        await event.respond("⚠️ Invalid code. Check and try again.", parse_mode="html")
        store.login.pop(uid, None)
        store.set_pending(uid, None)
        try:
            await state.client.disconnect()
        except Exception:
            pass
        return True
    except PhoneCodeExpiredError:
        await event.respond("⚠️ The code has expired. Restart with /start → Accounts → Add.", parse_mode="html")
        store.login.pop(uid, None)
        store.set_pending(uid, None)
        try:
            await state.client.disconnect()
        except Exception:
            pass
        return True
    except Exception as exc:
        log.warning("sign_in failed: %s", exc)
        await event.respond("⚠️ Sign-in failed. Try again or restart with /start → Accounts → Add.", parse_mode="html")
        store.login.pop(uid, None)
        store.set_pending(uid, None)
        try:
            await state.client.disconnect()
        except Exception:
            pass
        return True
    await _finalize_login(bot, event, uid, state)
    return True


async def _on_password(bot, event, uid: int) -> bool:
    state = store.login.get(uid)
    if not state or not state.client:
        store.set_pending(uid, None)
        return True
    try:
        await asyncio.wait_for(
            state.client.sign_in(password=event.raw_text), timeout=config.OP_TIMEOUT
        )
    except Exception as exc:
        log.warning("2FA sign-in failed: %s", exc)
        await event.respond("⚠️ Wrong password. Try again.", parse_mode="html")
        store.login.pop(uid, None)
        store.set_pending(uid, None)
        try:
            await state.client.disconnect()
        except Exception:
            pass
        return True
    await _finalize_login(bot, event, uid, state)
    return True


async def _finalize_login(bot, event, uid: int, state: LoginState) -> None:
    client = state.client
    me = await asyncio.wait_for(client.get_me(), timeout=config.OP_TIMEOUT)
    session_string = client.session.save()
    name = getattr(me, "first_name", "") or getattr(me, "username", "") or state.phone
    display = f"{name}" if not getattr(me, "username", None) else f"{name} (@{me.username})"
    acc = await session_manager.add_account(
        uid, state.phone, display, session_string, getattr(me, "id", 0)
    )
    await db.set_active_sid(uid, acc["sid"])
    try:
        await client.disconnect()
    except Exception:
        pass
    store.login.pop(uid, None)
    store.set_pending(uid, None)
    accounts = await db.get_user_sessions(uid)
    active = await db.get_active_sid(uid)
    await event.respond(
        text.login_success(display, state.phone),
        buttons=keyboards.accounts_menu_keyboard(accounts, active),
        parse_mode="html",
    )
