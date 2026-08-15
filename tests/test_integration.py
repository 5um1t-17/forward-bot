"""Integration test: drive the transfer wizard via callback/text routing."""
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
    def __init__(self, id, media=None, text=""):
        self.id = id
        self.media = media
        self.message = text
        self.grouped_id = None
        self.reply_to = None
        self.entities = None
        self.forward = None

    @property
    def text(self):
        return self.message


class FakeEntity:
    def __init__(self, id, title="Chat"):
        self.id = id
        self.title = title


class FakeClient:
    def __init__(self):
        self.msgs = [FakeMessage(i, text=f"m{i}") for i in range(1, 61)]

    async def get_entity(self, id_or_username):
        if isinstance(id_or_username, str):
            return FakeEntity(-100111, "source chat")
        return FakeEntity(id_or_username, "dest chat")

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

    async def iter_dialogs(self, limit=0):
        for d in await self.get_dialogs(limit=limit):
            yield d

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


class _Dialog:
    def __init__(self, entity):
        self.entity = entity
        self.name = entity.title


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, buttons=None, parse_mode=None):
        self.sent.append(("send", uid, text, buttons))
        m = FakeMessage(1000)
        m.id = 1000
        return m

    async def edit_message(self, uid, msg_id, text, buttons=None, parse_mode=None):
        self.sent.append(("edit", uid, msg_id, text, buttons))


class FakeEvent:
    def __init__(self, sender_id, data=None, raw_text=None, message=None):
        self.sender_id = sender_id
        self.data = data.encode() if isinstance(data, str) else data
        self.raw_text = raw_text or ""
        self.message = message or FakeMessage(0, text=self.raw_text)
        self.edited = []

    async def edit(self, text, buttons=None, parse_mode=None):
        self.edited.append(("edit", text, buttons, parse_mode))

    async def answer(self, text=None, alert=False):
        self.edited.append(("answer", text, alert))

    async def respond(self, text, buttons=None, parse_mode=None):
        self.edited.append(("respond", text, buttons, parse_mode))


async def main():
    import bot.client_pool as cp_mod
    import bot.entity_resolver as er_mod
    from bot.handlers import accounts, transfer

    await db.init()
    # seed user with an active account session
    await db.set_active_sid(1, "test_sid")
    await db.add_session(1, "+1555", "Test User", "dummy_encrypted", 1)
    await db.set_setting(1, "threads", 2)

    # patch pool + dialog fetch
    async def fake_pool_get(uid, sid):
        return FakeClient()

    cp_mod.client_pool.get = fake_pool_get

    bot = FakeBot()

    ev = FakeEvent(1, data="tr:start")
    ok = await transfer.handle(bot, ev, "tr:start")
    assert ok and ev.edited[-1][1].startswith("📥"), ev.edited

    ev = FakeEvent(1, data="tr:src:any")
    await transfer.handle(bot, ev, "tr:src:any")
    assert store.pending(1) == "tr_source"

    # user sends the source as a link
    ev = FakeEvent(1, raw_text="@mysource")
    ok = await transfer.handle_pending(bot, ev, "tr_source")
    assert ok
    assert store.pending(1) is None
    wiz = store.get_transfer(1)
    assert wiz.source == {"id": -100111, "name": "source chat"}, wiz.source

    ev = FakeEvent(1, data="tr:src:ok")
    await transfer.handle(bot, ev, "tr:src:ok")
    wiz = store.get_transfer(1)
    if len(wiz.dialogs) < 2:
        pass  # debug removed for Windows console compatibility
    assert len(wiz.dialogs) >= 2, wiz.dialogs

    ev = FakeEvent(1, data=f"dst:sel:{wiz.dialogs[0]['id']}")
    await transfer.handle(bot, ev, f"dst:sel:{wiz.dialogs[0]['id']}")
    assert wiz.dest is not None

    ev = FakeEvent(1, data="tr:dst:ok")
    await transfer.handle(bot, ev, "tr:dst:ok")

    ev = FakeEvent(1, data="tr:count:50")
    await transfer.handle(bot, ev, "tr:count:50")
    assert wiz.count == 50

    ev = FakeEvent(1, data="tr:mode:copy")
    await transfer.handle(bot, ev, "tr:mode:copy")
    assert wiz.mode == "copy"

    ev = FakeEvent(1, data="tr:opt:hide_header")
    await transfer.handle(bot, ev, "tr:opt:hide_header")
    assert "hide_header" in wiz.options

    ev = FakeEvent(1, data="tr:opt:done")
    await transfer.handle(bot, ev, "tr:opt:done")

    ev = FakeEvent(1, data="tr:filter:all")
    await transfer.handle(bot, ev, "tr:filter:all")
    assert wiz.filter_type == "all"

    ev = FakeEvent(1, data="tr:filter:done")
    await transfer.handle(bot, ev, "tr:filter:done")

    ev = FakeEvent(1, data="tr:dedup:done")
    await transfer.handle(bot, ev, "tr:dedup:done")

    ev = FakeEvent(1, data="tr:sched:now")
    await transfer.handle(bot, ev, "tr:sched:now")
    summary_text = ev.edited[-1][1]
    assert "Transfer Summary" in summary_text, summary_text

    # run the transfer
    ev = FakeEvent(1, data="tr:run:start")
    await transfer.handle(bot, ev, "tr:run:start")
    sends = [s for s in bot.sent if s[0] == "send"]
    assert sends, "expected progress message"
    edits = [s for s in bot.sent if s[0] == "edit"]
    assert edits, "expected final edit"
    final = edits[-1][3]
    assert "Transfer Complete" in final or "interrupted" in final, final

    # save as job
    ev = FakeEvent(1, data="tr:run:savejob")
    await transfer.handle(bot, ev, "tr:run:savejob")
    assert store.pending(1) == "tr_job_name"
    ev = FakeEvent(1, raw_text="my job")
    ok = await transfer.handle_pending(bot, ev, "tr_job_name")
    assert ok
    jobs = await db.user_jobs(1)
    assert any(j["name"] == "my job" for j in jobs), jobs
    my_job = next(j for j in jobs if j["name"] == "my job")

    print("wizard flow OK; jobs:", len(jobs))

    # verify job run route (jobs handler)
    from bot.handlers import jobs as jobs_handler
    jid = str(my_job["_id"])
    ev = FakeEvent(1, data=f"jobs:run:{jid}")
    ok = await jobs_handler.handle(bot, ev, f"jobs:run:{jid}")
    assert ok

    if db.client is not None:
        db.client.close()
    print("INTEGRATION TESTS PASSED")


asyncio.run(main())
