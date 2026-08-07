"""Pool of connected Telethon user clients (one per account).

Clients are created lazily from decrypted sessions and reused for the lifetime
of the process, so transfers start instantly after the first use.
"""
from __future__ import annotations

import asyncio
import contextlib
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
        # refcount of active runs per account key. ``sweep`` skips clients that
        # are currently driving a transfer, so a healthy-but-busy client is
        # never disconnected just because its ping happened to be slow.
        self._in_use: dict[tuple[int, str], int] = {}

    def _lock(self, key: tuple[int, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @contextlib.asynccontextmanager
    async def use(self, user_id: int, sid: str):
        """Mark an account's client as in use for the duration of a run.

        Wrap ``engine.run(...)`` with this so the periodic pool sweep does not
        disconnect a client that is actively transferring.
        """
        key = (user_id, sid)
        self._in_use[key] = self._in_use.get(key, 0) + 1
        try:
            yield
        finally:
            remaining = self._in_use.get(key, 0) - 1
            if remaining <= 0:
                self._in_use.pop(key, None)
            else:
                self._in_use[key] = remaining

    def in_use(self, key: tuple[int, str]) -> bool:
        return self._in_use.get(key, 0) > 0

    async def _is_usable(self, client, timeout: float = 4.0) -> bool:
        """True if ``client`` answers a lightweight RPC.

        ``is_connected()`` only reflects the last socket state — a half-dead
        MTProto link can still report connected. Doing a real round-trip before
        handing a cached client back prevents every caller from silently
        hanging on a dead connection (e.g. destination chat loading).
        """
        try:
            await asyncio.wait_for(client(GetNearestDcRequest()), timeout=timeout)
            return True
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            return False

    async def get(self, user_id: int, sid: str, timeout: float | None = None):
        key = (user_id, sid)
        client = self._clients.get(key)
        if client is not None and client.is_connected() and await self._is_usable(client):
            return client
        if client is not None:
            self._clients.pop(key, None)
            try:
                await asyncio.wait_for(client.disconnect(), timeout=config.CLIENT_CONNECT_TIMEOUT)
            except Exception:
                pass
        async with self._lock(key):
            client = self._clients.get(key)
            if client is not None and client.is_connected() and await self._is_usable(client):
                return client
            if client is not None:
                self._clients.pop(key, None)
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=config.CLIENT_CONNECT_TIMEOUT)
                except Exception:
                    pass
            session_string = await asyncio.wait_for(
                session_manager.decrypt_session(user_id, sid),
                timeout=timeout or config.CLIENT_CONNECT_TIMEOUT,
            )
            if session_string is None:
                raise ValueError("Session not found or failed to decrypt")
            client = session_manager.build_client(session_string)
            try:
                await asyncio.wait_for(client.connect(), timeout=config.CLIENT_CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=config.CLIENT_CONNECT_TIMEOUT)
                except Exception:
                    pass
                raise ValueError("Telegram connection timed out — try again in a moment")
            try:
                authorized = await asyncio.wait_for(
                    client.is_user_authorized(), timeout=config.CLIENT_CONNECT_TIMEOUT
                )
            except asyncio.TimeoutError:
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=config.CLIENT_CONNECT_TIMEOUT)
                except Exception:
                    pass
                raise ValueError("Telegram connection timed out — try again in a moment")
            if not authorized:
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=config.CLIENT_CONNECT_TIMEOUT)
                except Exception:
                    pass
                raise ValueError("Session is no longer authorized — please re-add the account")
            self._clients[key] = client
            await asyncio.wait_for(db.touch_session(sid), timeout=config.CLIENT_CONNECT_TIMEOUT)
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
            if self.in_use(key):
                # Actively driving a transfer: a slow ping is expected and is
                # not a reason to disconnect a healthy-but-busy client.
                continue
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
