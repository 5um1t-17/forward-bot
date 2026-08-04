"""Pool of connected Telethon user clients (one per account).

Clients are created lazily from decrypted sessions and reused for the lifetime
of the process, so transfers start instantly after the first use.
"""
from __future__ import annotations

import asyncio
import logging

from telethon.tl.functions.account import UpdateStatusRequest

from bot.db import db
from bot.session_manager import session_manager

log = logging.getLogger("bot.pool")


class ClientPool:
    def __init__(self) -> None:
        self._clients: dict[tuple[int, str], object] = {}

    async def get(self, user_id: int, sid: str):
        key = (user_id, sid)
        client = self._clients.get(key)
        if client is not None:
            return client
        session_string = await session_manager.decrypt_session(user_id, sid)
        if session_string is None:
            raise ValueError("Session not found or failed to decrypt")
        client = session_manager.build_client(session_string)
        try:
            await asyncio.wait_for(client.connect(), timeout=20)
        except asyncio.TimeoutError:
            raise ValueError("Telegram connection timed out — try again in a moment")
        if not await client.is_user_authorized():
            raise ValueError("Session is no longer authorized — please re-add the account")
        self._clients[key] = client
        await db.touch_session(sid)
        return client

    async def refresh(self, user_id: int, sid: str):
        """Reconnect an existing pooled client (e.g. after it was disconnected)."""
        key = (user_id, sid)
        if key in self._clients:
            try:
                await self._clients[key].disconnect()
            except Exception:
                pass
            self._clients.pop(key, None)
        return await self.get(user_id, sid)

    async def dispose(self, user_id: int, sid: str) -> None:
        key = (user_id, sid)
        client = self._clients.pop(key, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def set_online(self, client, online: bool = True) -> None:
        try:
            await client(UpdateStatusRequest(offline=not online))
        except Exception:
            pass


client_pool = ClientPool()
