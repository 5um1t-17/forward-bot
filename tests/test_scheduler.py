"""Scheduler end-to-end test with mocked Telegram clients."""
import asyncio
import os

os.environ.setdefault("API_ID", "0")
os.environ.setdefault("API_HASH", "x")
os.environ.setdefault("BOT_TOKEN", "x")

from telethon.tl import types  # noqa: E402

from bot.client_pool import client_pool  # noqa: E402
from bot.db import db  # noqa: E402
from bot.scheduler import Scheduler, _compute_next, schedule_instant  # noqa: E402
from bot.transfer_engine import TransferEngine  # noqa: E402


class FakeMessage:
    def __init__(self, id, media=None, text=""):
        self.id = id
        self.media = media
        self.message = text
        self.grouped_id = None
        self.reply_to = None
        self.entities = None

    @property
    def text(self):
        return self.message


class FakeEntity:
    def __init__(self, id):
        self.id = id


class FakeClient:
    def __init__(self):
        self.msgs = [FakeMessage(i) for i in range(1, 21)]

    async def get_entity(self, id):
        return FakeEntity(id)

    async def get_messages(self, source, ids=None):
        by_id = {m.id: m for m in self.msgs}
        if isinstance(ids, (list, tuple, set)):
            return [by_id[i] for i in ids if i in by_id]
        return by_id.get(ids)

    async def forward_messages(self, dest, ids, from_peer=None, **kw):
        sent = [FakeMessage(i + 500) for i in ids]
        return sent if len(sent) > 1 else sent[0]

    async def send_file(self, dest, file, **kw):
        if isinstance(file, list):
            return [FakeMessage(9000 + i) for i in range(len(file))]
        return FakeMessage(8000)

    async def send_message(self, dest, text, **kw):
        return FakeMessage(7000)

    async def iter_messages(self, entity):
        for m in reversed(self.msgs):
            yield m

    async def connect(self):
        return True

    async def is_user_authorized(self):
        return True

    async def disconnect(self):
        return True


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, **kw):
        self.sent.append((uid, text))
        return FakeMessage(1)


async def main():
    await db.init()
    await db.set_setting(1, "notifications", True)

    # fake the account client pool (the scheduler builds clients via the pool)
    scheduler = Scheduler(FakeBot(), TransferEngine())

    async def fake_get(user_id, sid):
        return FakeClient()

    client_pool.get = fake_get

    job = {
        "user_id": 1,
        "sid": "abc",
        "name": "daily digests",
        "source": {"id": -100, "name": "Src"},
        "dest": {"id": -200, "name": "Dst"},
        "count_mode": "latest",
        "count": 10,
        "mode": "forward",
        "options": ["keep_sender"],
        "filter_type": "all",
        "dedup": False,
        "threads": 2,
        "forward_delay": 0,
        "retry_count": 3,
        "handle_flood": True,
        "auto_resume": True,
        "schedule_kind": "daily",
        "schedule_time": "09:00",
        "schedule_weekday": None,
        "status": "scheduled",
        "next_run": schedule_instant("later", "00:00"),
    }
    jid = await db.save_job(job)
    await db.mark_running(jid)

    await scheduler.execute_job(await db.get_job(jid))

    updated = await db.get_job(jid)
    assert updated["status"] == "scheduled", updated
    assert updated["last_run"] is not None
    assert updated.get("last_summary", {}).get("success") == 10, updated

    logs = await db.user_logs(1)
    assert len(logs) >= 1
    assert logs[0]["status"] == "done" and logs[0]["success"] == 10, logs[0]

    notified = [t for _, t in scheduler.bot.sent if "Transfer Complete" in t]
    assert notified, "expected completion notification"
    print("scheduler execute_job OK; notification sent")

    # schedule date logic
    from datetime import datetime, timezone
    base = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)  # a Saturday
    assert _compute_next("weekly", "09:00", 1, base).weekday() == 1  # next Monday
    print("schedule date logic OK")

    if db.client is not None:
        db.client.close()
    print("SCHEDULER TESTS PASSED")


asyncio.run(main())
