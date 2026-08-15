"""Account + session management.

Sessions are stored as encrypted Telethon StringSession payloads in MongoDB.
"""
from __future__ import annotations

import logging

from telethon import TelegramClient
from telethon.sessions import StringSession

from bot.config import config
from bot.crypto import SessionCrypto
from bot.db import db

log = logging.getLogger("bot.session")


class SessionManager:
    def __init__(self) -> None:
        self.crypto = SessionCrypto()

    # ------------------------------------------------------------------
    def build_client(self, session_string: str | None = None) -> TelegramClient:
        session = StringSession(session_string) if session_string else StringSession()
        return TelegramClient(session, config.API_ID, config.API_HASH)

    # ------------------------------------------------------------------
    async def add_account(
        self, user_id: int, phone: str, name: str, session_string: str, tg_user_id: int
    ) -> dict:
        encrypted = self.crypto.encrypt(session_string)
        return await db.add_session(user_id, phone, name, encrypted, tg_user_id)

    async def decrypt_session(self, user_id: int, sid: str) -> str | None:
        doc = await db.get_session(user_id, sid)
        if not doc:
            return None
        try:
            return self.crypto.decrypt(doc["encrypted_session"])
        except ValueError:
            log.exception("Failed to decrypt session %s for user %s", sid, user_id)
            return None

    async def get_decrypted_all(self, user_id: int) -> list[dict]:
        """Return user sessions with decrypted session strings attached."""
        docs = await db.get_user_sessions(user_id)
        result = []
        for doc in docs:
            try:
                doc["_session_string"] = self.crypto.decrypt(doc["encrypted_session"])
                result.append(doc)
            except ValueError:
                log.warning("Skipping corrupted session %s for user %s", doc.get("sid"), user_id)
        return result


session_manager = SessionManager()
