"""Pool of connected Telethon user clients (one per account).

Clients are created lazily from decrypted sessions and reused for the lifetime
of the process, so transfers start instantly after the first use.
"""
from __future__ import annotations

import asyncio
import logging

from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.help import GetNearestDcRequest

from bot.config import config
from bot.db import db
from bot.session_manager import session_manager

log = logging.getLogger("bot.pool")


async def client_alive(client, timeout: float = 5.0) -> bool:
    """True if ``client`` answers a lightweight RPC within ``timeout``.

    ``is_connected()`` only reflects the last socket state, so a half-dead
    MTProto link can still report connected. A real round-trip detects it.
    """
    try:
        await asyncio.wait_for(client(GetNearestDcRequest()), timeout=timeout)
        return True
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        return False


class ClientPool:
    def __init__(self) -> None:
        self._clients: dict[tuple[int, str], object] = {}
        # one lock per account so concurrent callbacks never spin up a second
        # client (and a second hung connection) while the first is connecting
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}

    def _lock(self, key: tuple[int, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def get(self, user_id: int, sid: str):
        key = (user_id, sid)
        client = self._clients.get(key)
        if client is not None and client.is_connected():
            return client
        # Only one client may ever exist per account: the per-key lock plus the
        # double-check guarantees concurrent callbacks never spin up a second
        # session (and a second hung connection) for the same account.
        async with self._lock(key):
            client = self._clients.get(key)
            if client is not None and client.is_connected():
                return client
            if client is not None:
                # pooled client dropped its connection — rebuild it fresh
                self._clients.pop(key, None)
                try:
                    await client.disconnect()
                except Exception:
                    pass
            session_string = await session_manager.decrypt_session(user_id, sid)
            if session_string is None:
                raise ValueError("Session not found or failed to decrypt")
            client = session_manager.build_client(session_string)
            try:
                await asyncio.wait_for(client.connect(), timeout=config.CLIENT_CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                raise ValueError("Telegram connection timed out — try again in a moment")
            try:
                authorized = await asyncio.wait_for(
                    client.is_user_authorized(), timeout=config.CLIENT_CONNECT_TIMEOUT
                )
            except asyncio.TimeoutError:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                raise ValueError("Telegram connection timed out — try again in a moment")
            if not authorized:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                raise ValueError("Session is no longer authorized — please re-add the account")
            self._clients[key] = client
            await db.touch_session(sid)
            return client

    async def refresh(self, user_id: int, sid: str):
        """Force-rebuild the pooled client for an account.

        Used by the engine's ``refresh_client`` hook after network failures:
        the stale client is dropped (even if it still reports connected, which
        half-dead MTProto clients do) and a brand new session is created.
        """
        key = (user_id, sid)
        if key in self._clients:
            try:
                await asyncio.wait_for(
                    self._clients[key].disconnect(), timeout=config.CLIENT_CONNECT_TIMEOUT
                )
            except Exception:
                pass
            self._clients.pop(key, None)
        return await self.get(user_id, sid)

    async def sweep(self, timeout: float = 5.0) -> int:
        """Drop pooled clients that are dead but still registered.

        ``is_connected()`` only reflects the last socket state — a client can
        report connected while its MTProto link is silently dead. ``sweep``
        issues a bounded lightweight RPC to every client and removes those
        that fail, so half-dead clients are never handed back to a transfer.
        Called periodically from :mod:`bot.main`.
        """
        removed = 0
        for key in list(self._clients):
            client = self._clients.get(key)
            if client is None:
                continue
            try:
                if not await client_alive(client, timeout=timeout):
                    raise ConnectionError("no response")
                continue  # healthy
            except asyncio.TimeoutError:
                log.warning("pool sweep: client %s did not answer ping", key)
            except Exception as exc:  # noqa: BLE001
                log.debug("pool sweep: client %s unhealthy: %s", key, exc)
            self._clients.pop(key, None)
            try:
                await asyncio.wait_for(
                    client.disconnect(), timeout=config.CLIENT_CONNECT_TIMEOUT
                )
            except Exception:
                pass
            removed += 1
            log.info("pool sweep: removed dead client for account %s", key)
        if removed:
            log.info("pool sweep removed %d dead client(s)", removed)
        return removed

    async def dispose(self, user_id: int, sid: str) -> None:
        key = (user_id, sid)
        client = self._clients.pop(key, None)
        if client is not None:
            try:
                await asyncio.wait_for(
                    client.disconnect(), timeout=config.CLIENT_CONNECT_TIMEOUT
                )
            except Exception:
                pass

    async def set_online(self, client, online: bool = True) -> None:
        try:
            await client(UpdateStatusRequest(offline=not online))
        except Exception:
            pass


client_pool = ClientPool()
