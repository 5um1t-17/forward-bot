"""Regression tests for the client pool (no live Telegram needed)."""
import asyncio
import os

os.environ.setdefault("API_ID", "0")
os.environ.setdefault("API_HASH", "x")
os.environ.setdefault("BOT_TOKEN", "x")

import bot.client_pool as cp  # noqa: E402


class FakeClient:
    def __init__(self):
        self.disconnected = False

    def is_connected(self):
        return True

    async def disconnect(self):
        self.disconnected = True

    async def __call__(self, request):
        return None


def test_in_use_blocks_sweep_removal():
    # A client that is actively driving a transfer must survive the sweep even
    # if its ping is slow/absent; once released it is swept normally.
    pool = cp.ClientPool()
    key = (1, "sid1")
    client = FakeClient()
    pool._clients[key] = client
    original_alive = cp.client_alive

    async def dead(*args, **kwargs):
        return False

    cp.client_alive = dead
    try:
        async def run():
            async with pool.use(1, "sid1"):
                assert pool.in_use(key)
                removed = await pool.sweep()
                assert removed == 0
                assert pool._clients.get(key) is client
            assert not pool.in_use(key)
            removed = await pool.sweep()
            assert removed == 1
            assert key not in pool._clients
            assert client.disconnected

        asyncio.run(run())
    finally:
        cp.client_alive = original_alive
    print("pool in-use sweep skip OK")


def test_use_refcount():
    pool = cp.ClientPool()

    async def run():
        async with pool.use(1, "s"):
            assert pool.in_use((1, "s"))
            async with pool.use(1, "s"):
                assert pool._in_use[(1, "s")] == 2
            assert pool.in_use((1, "s"))
        assert not pool.in_use((1, "s"))
        assert (1, "s") not in pool._in_use

    asyncio.run(run())
    print("pool use refcount OK")


test_in_use_blocks_sweep_removal()
test_use_refcount()
print("CLIENT POOL TESTS PASSED")
