"""Targeted test: destination loading must never leave the user stuck.

When the callback message cannot be edited (deleted, or the click landed on a
message the bot does not own), the destination step must fall back to sending
a fresh message so the user always sees either the chat list or a recovery
prompt instead of a stale "Loading destination chats..." screen.
"""
import asyncio
import os
from datetime import datetime, timezone

os.environ.setdefault("API_ID", "0")
os.environ.setdefault("API_HASH", "x")
os.environ.setdefault("BOT_TOKEN", "x")

from telethon.tl import types  # noqa: E402

from bot.db import db  # noqa: E402
from bot.state import store  # noqa: E402


class FakeMessage:
    def __init__(self, id, text=""):
        self.id = id
        self.message = text
        self.forward = None

    @property
    def text(self):
        return self.message


class FakeEntity:
    def __init__(self, id, title="Chat"):
        self.id = id
        self.title = title


class _Dialog:
    def __init__(self, entity):
        self.entity = entity
        self.name = entity.title


class FakeClient:
    async def get_entity(self, id_or_username):
        return FakeEntity(-100111, "source chat")

    async def get_dialogs(self, limit=0):
        now_dt = datetime.now(timezone.utc)
        out = []
        for cid, title in ((-100222, "Dest Channel"), (-100333, "Dest Group")):
            ch = types.Channel(
                id=cid,
                title=title,
                photo=None,
                date=now_dt,
                access_hash=0,
                participants_count=0,
                broadcast=(cid == -100222),
                megagroup=(cid != -100222),
                creator=True,
                admin_rights=types.ChatAdminRights(post_messages=True),
            )
            out.append(_Dialog(ch))
        return out

    async def connect(self):
        return True

    async def is_user_authorized(self):
        return True


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, buttons=None, parse_mode=None):
        self.sent.append(("send", uid, text, buttons))
        return FakeMessage(1000)


class FailingEditEvent:
    """Callback event whose edit always raises, like a deleted/foreign message."""

    def __init__(self, sender_id):
        self.sender_id = sender_id
        self.answered = False

    async def edit(self, text, buttons=None, parse_mode=None):
        raise RuntimeError("MESSAGE_ID_INVALID")

    async def answer(self, text=None, alert=False):
        self.answered = True


async def run():
    import bot.client_pool as cp_mod
    from bot.handlers import transfer

    await db.init()
    await db.set_active_sid(1, "test_sid")
    await db.add_session(1, "+1555", "Test User", "dummy_encrypted", 1)

    async def fake_pool_get(uid, sid):
        return FakeClient()

    cp_mod.client_pool.get = fake_pool_get

    bot = FakeBot()

    # 1) Edit failure while listing chats -> fresh message with the list.
    ev = FailingEditEvent(1)
    ok = await transfer._ask_dest(bot, ev, 1)
    assert ok, "handler must report handled even when edit fails"
    sends = [s for s in bot.sent if s[0] == "send"]
    assert sends, "expected a fresh message when edit fails"
    final_kb = sends[-1][3]
    assert final_kb, "expected destination keyboard in fallback message"
    flat = [b.text for row in final_kb for b in row]
    assert "Dest Channel" in flat and "Dest Group" in flat, flat

    # 2) Edit failure with no dialogs -> fresh recovery prompt, not a hang.
    async def no_dialogs(client, limit=200):
        return []

    original_fetch = transfer.fetch_sendable_dialogs
    transfer.fetch_sendable_dialogs = no_dialogs
    bot.sent.clear()
    store.reset_transfer(1)
    ev = FailingEditEvent(1)
    await transfer._ask_dest(bot, ev, 1)
    sends = [s for s in bot.sent if s[0] == "send"]
    assert sends, "expected fallback message for empty dialogs"
    assert "No groups/channels found" in sends[-1][2], sends[-1][2]
    transfer.fetch_sendable_dialogs = original_fetch

    # 3) Normal path (editable message) must keep using edits, no fallback.
    class NormalEvent(FailingEditEvent):
        def __init__(self, sender_id):
            super().__init__(sender_id)
            self.edited = []

        async def edit(self, text, buttons=None, parse_mode=None):
            self.edited.append((text, buttons))

    bot.sent.clear()
    store.reset_transfer(1)
    ev = NormalEvent(1)
    await transfer._ask_dest(bot, ev, 1)
    assert not bot.sent, "normal path must not send a fresh message"
    assert ev.edited, "normal path must edit the callback message"
    kb = ev.edited[-1][1]
    assert kb, "expected destination keyboard in edited message"
    flat = [b.text for row in kb for b in row]
    assert "Dest Channel" in flat, flat

    # 4) Flood-limited bot: edit and send must both be skipped, and the user
    #    gets an alert instead of a silent stuck screen.
    from bot.handlers import common as common_mod

    original_note = common_mod.note_flood
    await common_mod.note_flood(3600)  # pretend Telegram demanded a 1h wait
    bot.sent.clear()
    store.reset_transfer(1)
    ev = FailingEditEvent(1)
    await transfer._ask_dest(bot, ev, 1)
    assert not bot.sent, "flood-limited bot must not attempt sends"
    assert ev.answered, "flood-limited bot must alert the user"
    common_mod._flood_until = 0.0
    common_mod.note_flood = original_note

    # 5) normal path again after the flood window has cleared
    bot.sent.clear()
    store.reset_transfer(1)
    ev = NormalEvent(1)
    await transfer._ask_dest(bot, ev, 1)
    assert not bot.sent
    assert ev.edited

    if db.client is not None:
        db.client.close()
    print("DEST FALLBACK TESTS PASSED")


asyncio.run(run())
