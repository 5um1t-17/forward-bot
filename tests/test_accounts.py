"""Test the account login flow with a mocked Telethon client."""
import asyncio
import os

os.environ.setdefault("API_ID", "0")
os.environ.setdefault("API_HASH", "x")
os.environ.setdefault("BOT_TOKEN", "x")

from bot.db import db  # noqa: E402
from bot.handlers import accounts  # noqa: E402
from bot.state import store  # noqa: E402
from bot.session_manager import session_manager  # noqa: E402


class FakeMe:
    first_name = "John"
    username = "john_doe"
    id = 999


class FakeAuthClient:
    def __init__(self):
        self.codes = {}

    async def connect(self):
        return True

    async def send_code_request(self, phone):
        self.codes["hash"] = "h1"
        return type("R", (), {"phone_code_hash": "h1"})()

    async def sign_in(self, phone=None, code=None, password=None, phone_code_hash=None):
        if password is None and code == "12345":
            raise __import__("telethon").errors.SessionPasswordNeededError(request=None)
        if password == "secret":
            return "ok"
        raise ValueError("bad code")

    async def get_me(self):
        return FakeMe()

    async def disconnect(self):
        return None

    @property
    def session(self):
        class S:
            def save(self):
                return "session-string-abc"
        return S()


class FakeMessageEvent:
    def __init__(self, sender_id, raw_text):
        self.sender_id = sender_id
        self.raw_text = raw_text
        self.responded = []

    async def respond(self, text, buttons=None, parse_mode=None):
        self.responded.append(text)


async def main():
    from telethon.errors import SessionPasswordNeededError
    from bot import db as dbmod
    from bot.session_manager import session_manager as sm

    await db.init()
    # reset user 1's test data
    await db.sessions.delete_many({"user_id": 1})
    await db.users.delete_many({"user_id": 1})

    # stub out client construction
    clients = []

    def fake_build_client(session_string=None):
        c = FakeAuthClient()
        clients.append(c)
        return c

    sm.build_client = fake_build_client

    bot = None
    # phone step
    ev = FakeMessageEvent(1, "+15551234567")
    ok = await accounts.handle_pending(bot, ev, "login_phone")
    assert ok and store.pending(1) == "login_code", store.pending(1)

    # code step -> triggers 2FA
    ev = FakeMessageEvent(1, "12345")
    ok = await accounts.handle_pending(bot, ev, "login_code")
    assert ok and store.pending(1) == "login_password", store.pending(1)

    # password step
    ev = FakeMessageEvent(1, "secret")
    ok = await accounts.handle_pending(bot, ev, "login_password")
    assert ok and store.pending(1) is None

    # session should be persisted & encrypted
    docs = await db.get_user_sessions(1)
    assert len(docs) == 1
    assert docs[0]["phone"] == "+15551234567"
    assert docs[0]["name"] == "John (@john_doe)"
    assert "session-string-abc" != docs[0]["encrypted_session"]
    decrypted = sm.crypto.decrypt(docs[0]["encrypted_session"])
    assert decrypted == "session-string-abc"

    assert await db.get_active_sid(1) == docs[0]["sid"]
    print("login flow OK; account persisted encrypted")

    if db.client is not None:
        db.client.close()
    print("ACCOUNTS TESTS PASSED")


asyncio.run(main())
