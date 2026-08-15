"""Regression tests for the bot health watchdog (no live Telegram needed)."""
import asyncio
import contextlib
import os
import time

os.environ.setdefault("API_ID", "0")
os.environ.setdefault("API_HASH", "x")
os.environ.setdefault("BOT_TOKEN", "x")

from telethon.tl.functions.help import GetNearestDcRequest  # noqa: E402

import bot.config as bconfig  # noqa: E402
from bot.handlers import common  # noqa: E402
from bot.main import _bot_alive, _bot_health_watchdog  # noqa: E402


class FakeBot:
    def __init__(self, ping_ok=True):
        self.ping_ok = ping_ok
        self.disconnected = False

    def is_connected(self):
        return True

    async def __call__(self, request):
        if not self.ping_ok:
            raise ConnectionError("simulated dead MTProto link")
        return {"ok": True}

    async def disconnect(self):
        self.disconnected = True


@contextlib.contextmanager
def fast_watchdog(interval: float = 0.01):
    old_interval = bconfig.config.BOT_WATCHDOG_INTERVAL
    old_skip = bconfig.config.BOT_ACTIVITY_SKIP
    old_ping = bconfig.config.BOT_PING_TIMEOUT
    old_until = common._flood_until
    common._flood_until = 0.0
    bconfig.config.BOT_WATCHDOG_INTERVAL = interval
    bconfig.config.BOT_ACTIVITY_SKIP = 90.0
    bconfig.config.BOT_PING_TIMEOUT = 15.0
    try:
        yield
    finally:
        common._flood_until = old_until
        bconfig.config.BOT_WATCHDOG_INTERVAL = old_interval
        bconfig.config.BOT_ACTIVITY_SKIP = old_skip
        bconfig.config.BOT_PING_TIMEOUT = old_ping


def test_bot_idle_tracking():
    common._last_bot_activity = 0.0
    assert common.bot_idle_seconds() > 90.0
    common.note_bot_activity()
    assert common.bot_idle_seconds() < 1.0
    common._last_bot_activity = 0.0


async def test_watchdog_skips_while_bot_active():
    # Even though the ping RPC always fails, a bot that has just answered a
    # real RPC must NOT be reconnected. This is the regression test for the
    # "3 consecutive failures every ~4 minutes during transfers" bug.
    bot = FakeBot(ping_ok=False)
    with fast_watchdog():
        common._last_bot_activity = time.monotonic()
        task = asyncio.create_task(_bot_health_watchdog(bot))
        await asyncio.sleep(0.15)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert not bot.disconnected, "active bot must never be force-reconnected"
    common._last_bot_activity = 0.0


async def test_watchdog_reconnects_when_idle_and_unresponsive():
    # An idle bot whose ping fails 3 consecutive times IS genuinely wedged and
    # must still be reconnected (the watchdog's original purpose).
    bot = FakeBot(ping_ok=False)
    with fast_watchdog():
        common._last_bot_activity = 0.0  # host uptime long -> idle
        await asyncio.wait_for(_bot_health_watchdog(bot), timeout=2.0)
        assert bot.disconnected
    common._last_bot_activity = 0.0


async def test_watchdog_reconnects_when_idle_but_responses_ok():
    # A healthy idle bot is never reconnected.
    bot = FakeBot(ping_ok=True)
    with fast_watchdog():
        common._last_bot_activity = 0.0
        task = asyncio.create_task(_bot_health_watchdog(bot))
        await asyncio.sleep(0.15)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert not bot.disconnected
    common._last_bot_activity = 0.0


async def test_watchdog_skips_while_flood_blocked():
    # A flood-limited bot pings spuriously; the watchdog must not count those.
    bot = FakeBot(ping_ok=False)
    with fast_watchdog():
        common._last_bot_activity = 0.0
        common._flood_until = time.monotonic() + 60.0
        task = asyncio.create_task(_bot_health_watchdog(bot))
        await asyncio.sleep(0.15)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert not bot.disconnected, "flood-limited bot must not be reconnected"
    common._last_bot_activity = 0.0


async def test_bot_alive_returns_true_on_flood_wait():
    from telethon.errors import FloodWaitError

    class FloodingBot(FakeBot):
        async def __call__(self, request):
            raise FloodWaitError(request, 30)

    bot = FloodingBot()
    with fast_watchdog():
        assert await _bot_alive(bot) is True


async def main():
    test_bot_idle_tracking()
    await test_watchdog_skips_while_bot_active()
    await test_watchdog_reconnects_when_idle_and_unresponsive()
    await test_watchdog_reconnects_when_idle_but_responses_ok()
    await test_watchdog_skips_while_flood_blocked()
    await test_bot_alive_returns_true_on_flood_wait()
    print("HEALTH WATCHDOG TESTS PASSED")


asyncio.run(main())
