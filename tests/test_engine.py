"""Standalone smoke tests (no live Telegram API needed)."""
import asyncio
import contextlib
import os
import tempfile
import time

os.environ.setdefault("API_ID", "0")
os.environ.setdefault("API_HASH", "x")
os.environ.setdefault("BOT_TOKEN", "x")

from telethon.tl import types  # noqa: E402
from telethon.errors import FloodWaitError  # noqa: E402

from bot.entity_resolver import parse_input  # noqa: E402
from bot.transfer_engine import TransferConfig, TransferEngine, message_matches_filter  # noqa: E402


class FakeMessage:
    def __init__(self, id, media=None, text="", grouped_id=None, reply_to=None, entities=None):
        self.id = id
        self.media = media
        self.message = text
        self.grouped_id = grouped_id
        self.reply_to = reply_to
        self.entities = entities

    @property
    def text(self):
        return self.message


class FakeEntity:
    def __init__(self, id):
        self.id = id


class FakeClient:
    def __init__(self, messages):
        self.messages = messages
        self.sent = []

    async def get_messages(self, source, ids=None):
        by_id = {m.id: m for m in self.messages}
        if isinstance(ids, (list, tuple, set)):
            return [by_id[i] for i in ids if i in by_id]
        return by_id.get(ids)

    async def forward_messages(self, dest, ids, from_peer=None, **kw):
        sent = []
        for i in ids:
            sent.append(FakeMessage(i + 1000))
        self.sent.append(("forward", ids, kw))
        return sent if len(sent) > 1 else sent[0]

    async def send_file(self, dest, file, **kw):
        if isinstance(file, list):
            sent = [FakeMessage(5000 + i) for i in range(len(file))]
            self.sent.append(("copy_album", len(file), kw))
            return sent
        self.sent.append(("copy_file", 1, kw))
        return FakeMessage(6000)

    async def send_message(self, dest, text, **kw):
        self.sent.append(("copy_text", text, kw))
        return FakeMessage(7000)

    async def iter_messages(self, entity):
        for m in reversed(sorted(self.messages, key=lambda x: x.id)):
            yield m


def test_parse_input():
    cases = [
        ("https://t.me/mychannel/123", {"kind": "message_link", "slug": "mychannel", "msg_id": 123}),
        ("@username", {"kind": "username", "username": "username"}),
        ("username", {"kind": "username", "username": "username"}),
        ("-1001234567890", {"kind": "channel_id", "id": -1001234567890}),
        ("https://t.me/c/123456789/50", {"kind": "message_link", "slug": "c", "cid": 123456789, "msg_id": 50}),
        ("t.me/abc", {"kind": "message_link", "slug": "abc", "msg_id": None}),
    ]
    for text, expected in cases:
        got = parse_input(text)
        for k, v in expected.items():
            assert got.get(k) == v, f"{text}: {got}"
    print("parse_input OK")


def test_filter():
    photo = FakeMessage(1, types.MessageMediaPhoto(photo=None))
    video = FakeMessage(2, types.MessageMediaDocument(
        document=types.Document(id=2, access_hash=1, file_reference=b"", date=None, mime_type="video/mp4", size=10, dc_id=1, attributes=[])))
    doc = FakeMessage(3, types.MessageMediaDocument(
        document=types.Document(id=3, access_hash=1, file_reference=b"", date=None, mime_type="application/pdf", size=10, dc_id=1, attributes=[])))
    txt = FakeMessage(4, text="hello")
    service = types.MessageService(id=5, peer_id=None, date=None, action=None)
    assert message_matches_filter(photo, "photo")
    assert not message_matches_filter(photo, "video")
    assert message_matches_filter(video, "video")
    assert message_matches_filter(video, "media")
    assert message_matches_filter(doc, "document")
    assert not message_matches_filter(doc, "video")
    assert message_matches_filter(txt, "text")
    assert message_matches_filter(service, "all") is False
    print("filters OK")


def test_album_grouping():
    src = FakeEntity(1)
    dst = FakeEntity(2)
    msgs = [
        FakeMessage(10, types.MessageMediaPhoto(photo=None), grouped_id=99),
        FakeMessage(11, types.MessageMediaPhoto(photo=None), grouped_id=99),
        FakeMessage(12, text="solo"),
        FakeMessage(13, types.MessageMediaPhoto(photo=None), grouped_id=100),
    ]
    client = FakeClient(msgs)
    eng = TransferEngine()
    items = eng._build_items(msgs, TransferConfig(source_entity=src, dest_entity=dst, message_ids=[], filter_type="all"))
    assert len(items) == 3, items
    assert len(items[0]["messages"]) == 2  # album of 10,11
    print("album grouping OK")


async def test_run_copy_forward():
    src = FakeEntity(1)
    dst = FakeEntity(2)
    msgs = [
        FakeMessage(10, types.MessageMediaPhoto(photo=None), grouped_id=99),
        FakeMessage(11, types.MessageMediaPhoto(photo=None), grouped_id=99),
        FakeMessage(12, text="solo"),
    ]
    client = FakeClient(msgs)
    eng = TransferEngine()

    cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=[10, 11, 12],
                         mode="forward", options={"keep_sender"}, threads=2,
                         dedup=False, sid="abc")
    res = await eng.run(client, cfg)
    assert res.total == 3 and res.success == 3 and res.failed == 0, res
    fwd_ids = [ids for op, ids, _ in client.sent if op == "forward"]
    assert [12] in fwd_ids and [10, 11] in fwd_ids, client.sent

    client2 = FakeClient(msgs)
    cfg2 = TransferConfig(source_entity=src, dest_entity=dst, message_ids=[10, 11, 12],
                          mode="copy", options={"hide_header"}, threads=2,
                          dedup=False, sid="abc")
    res2 = await eng.run(client2, cfg2)
    assert res2.success == 3, res2
    assert any(op == "copy_album" for op, _, _ in client2.sent), client2.sent
    print("run forward/copy OK")


async def test_dedup_skip():
    from bot.db import db
    await db.init()
    src, dst = FakeEntity(1), FakeEntity(2)
    msgs = [FakeMessage(10, text="dup")]
    client = FakeClient(msgs)
    await db.mark_transferred(1, 2, 10, "abc", "copy")
    eng = TransferEngine()
    cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=[10],
                         mode="copy", dedup=True, sid="abc")
    res = await eng.run(client, cfg)
    assert res.skipped == 1 and res.success == 0, res
    print("dedup skip OK")


async def test_stop_no_hang():
    src = FakeEntity(1)
    dst = FakeEntity(2)
    msgs = [FakeMessage(i, text=f"m{i}") for i in range(1, 201)]

    class SlowClient(FakeClient):
        async def forward_messages(self, dest, ids, from_peer=None, **kw):
            await asyncio.sleep(0.02)
            sent = [FakeMessage(i + 1000) for i in ids]
            return sent if len(sent) > 1 else sent[0]

    client = SlowClient(msgs)
    eng = TransferEngine()

    async def stopper():
        await asyncio.sleep(0.2)
        eng.request_stop()

    cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=list(range(1, 201)),
                         mode="forward", options={"keep_sender"}, threads=4, dedup=False, sid="abc")
    stop_task = asyncio.create_task(stopper())
    result = await asyncio.wait_for(eng.run(client, cfg), timeout=10)
    await stop_task
    assert result.cancelled, result
    assert result.error == "stopped by user", result
    print(f"stop no-hang OK (processed {result.success} before stop)")


def _media_msg(mid: int):
    return FakeMessage(mid, types.MessageMediaDocument(
        document=types.Document(id=mid, access_hash=1, file_reference=b"", date=None,
                                mime_type="video/mp4", size=10, dc_id=1, attributes=[])))


class DownloadClient(FakeClient):
    def __init__(self, messages, delays=None, upload_delays=None):
        super().__init__(messages)
        self.delays = delays or {}
        self.upload_delays = upload_delays or {}
        self.upload_order = []
        self.upload_ready_order = []
        # strict order-of-events log: ("dl", msg_id) when a download starts,
        # ("up", msg_id) when that item's upload finishes
        self.sequence = []

    async def download_media(self, msg, file=None, progress_callback=None):
        self.sequence.append(("dl", msg.id))
        if self.delays.get(msg.id):
            await asyncio.sleep(self.delays[msg.id])
        with open(file, "w") as f:
            f.write(str(msg.id))
        return file

    async def upload_file(self, file, progress_callback=None):
        with open(file) as f:
            mid = int(f.read())
        delay = self.upload_delays.get(mid)
        if delay:
            await asyncio.sleep(delay)
        self.upload_ready_order.append(mid)
        return file

    async def send_file(self, dest, file, **kw):
        if isinstance(file, list):
            ids = []
            for p in file:
                with open(p) as f:
                    ids.append(int(f.read()))
                self.upload_order.append(ids[-1])
                self.sequence.append(("up", ids[-1]))
            sent = [FakeMessage(8000 + i) for i in range(len(ids))]
            self.sent.append(("dl_album", ids, kw))
            return sent
        with open(file) as f:
            mid = int(f.read())
        self.upload_order.append(mid)
        self.sequence.append(("up", mid))
        self.sent.append(("dl_file", mid, kw))
        return FakeMessage(9000 + mid)

    async def send_message(self, dest, text, **kw):
        self.sent.append(("dl_text", text, kw))
        try:
            mid = int(str(text).strip())
        except ValueError:
            mid = -1
        self.upload_order.append(mid)
        self.sequence.append(("up", mid))
        return FakeMessage(7000)


async def test_download_mixed_order():
    """A leading text message must be uploaded before the media that follows,
    i.e. uploads land in the exact source order even across text/media."""
    src = FakeEntity(1)
    dst = FakeEntity(2)
    msgs = [
        FakeMessage(1, text="1"),   # text first
        _media_msg(2),
        _media_msg(3),
        FakeMessage(4, text="4"),
        _media_msg(5),
    ]
    delays = {2: 0.12, 3: 0.08, 5: 0.04}  # media slower, so unordered work would reorder
    client = DownloadClient(msgs, delays)
    eng = TransferEngine()

    cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=[1, 2, 3, 4, 5],
                         mode="download", threads=4, dedup=False, sid="abc")
    res = await eng.run(client, cfg)
    assert res.success == 5 and res.failed == 0, res
    assert client.upload_order == [1, 2, 3, 4, 5], client.upload_order
    print("download mixed text/media order OK")


async def test_forward_strict_order():
    """Forward mode must land messages in the exact source order (no mixing),
    including albums and interleaved text/media."""
    src = FakeEntity(1)
    dst = FakeEntity(2)
    msgs = [
        FakeMessage(1, text="t1"),
        FakeMessage(2, types.MessageMediaPhoto(photo=None), grouped_id=77),
        FakeMessage(3, types.MessageMediaPhoto(photo=None), grouped_id=77),
        FakeMessage(4, text="t4"),
        FakeMessage(5, types.MessageMediaPhoto(photo=None)),
    ]
    client = FakeClient(msgs)
    eng = TransferEngine()

    cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=[1, 2, 3, 4, 5],
                         mode="forward", options={"keep_sender"}, threads=1, dedup=False, sid="abc")
    res = await eng.run(client, cfg)
    assert res.success == 5 and res.failed == 0, res
    fwd_ids = [ids for op, ids, _ in client.sent if op == "forward"]
    assert fwd_ids == [[1], [2, 3], [4], [5]], fwd_ids
    print("forward strict order OK")


async def test_download_ordered_pipeline():
    """Uploads must land in source order even when the first file is slow."""
    src = FakeEntity(1)
    dst = FakeEntity(2)
    msgs = [_media_msg(i) for i in range(1, 11)]
    delays = {1: 0.15, 2: 0.08, 3: 0.04}
    client = DownloadClient(msgs, delays)
    eng = TransferEngine()

    cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=list(range(1, 11)),
                         mode="download", threads=4, dedup=False, sid="abc")
    res = await eng.run(client, cfg)
    assert res.success == 10 and res.failed == 0, res
    assert client.upload_order == list(range(1, 11)), client.upload_order
    assert len(os.listdir(tempfile.gettempdir())) >= 0  # temp files cleaned up
    leftovers = [f for f in os.listdir(tempfile.gettempdir()) if f.startswith("fwd_")]
    assert not leftovers, leftovers
    print("download ordered pipeline OK")


async def test_download_strict_serial():
    """One download and one upload at a time: item i's upload must finish
    before item i+1's download starts (strict serial lifecycle, no overlap),
    while sends still land in exact source order."""
    src = FakeEntity(1)
    dst = FakeEntity(2)
    msgs = [_media_msg(i) for i in range(1, 9)]
    client = DownloadClient(msgs, upload_delays={1: 0.3})
    eng = TransferEngine()

    cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=list(range(1, 9)),
                         mode="download", threads=4, dedup=False, sid="abc")
    res = await eng.run(client, cfg)
    assert res.success == 8 and res.failed == 0, res
    assert client.upload_order == list(range(1, 9)), client.upload_order
    assert client.sequence == [("dl", 1), ("up", 1), ("dl", 2), ("up", 2),
                               ("dl", 3), ("up", 3), ("dl", 4), ("up", 4),
                               ("dl", 5), ("up", 5), ("dl", 6), ("up", 6),
                               ("dl", 7), ("up", 7), ("dl", 8), ("up", 8)], client.sequence
    print("download strict serial OK")


async def test_download_pipeline_stop():
    src = FakeEntity(1)
    dst = FakeEntity(2)
    msgs = [_media_msg(i) for i in range(1, 200)]
    client = DownloadClient(msgs, delays={i: 0.05 for i in range(1, 200)})
    eng = TransferEngine()

    async def stopper():
        await asyncio.sleep(0.25)
        eng.request_stop()

    cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=list(range(1, 200)),
                         mode="download", threads=4, dedup=False, sid="abc")
    stop_task = asyncio.create_task(stopper())
    result = await asyncio.wait_for(eng.run(client, cfg), timeout=10)
    await stop_task
    assert result.cancelled, result
    print(f"download pipeline stop OK (uploaded {len(client.upload_order)})")


async def test_download_flood_cap():
    """Escalating FloodWait must never hang the run forever: once the
    cumulative wait for an operation exceeds the cap, the item is counted as
    failed and the pipeline moves on."""
    import bot.config as bconfig
    old_sleep, old_cap, old_buf = (
        bconfig.config.MAX_FLOOD_SLEEP, bconfig.config.MAX_FLOOD_WAIT, bconfig.config.FLOOD_BUFFER,
    )
    bconfig.config.MAX_FLOOD_SLEEP, bconfig.config.MAX_FLOOD_WAIT, bconfig.config.FLOOD_BUFFER = 0.01, 0.05, 0.01
    try:
        src = FakeEntity(1)
        dst = FakeEntity(2)
        msgs = [_media_msg(i) for i in range(1, 4)]

        class FloodClient(DownloadClient):
            async def send_file(self, dest, file, **kw):
                raise FloodWaitError(None, 5)

        client = FloodClient(msgs)
        eng = TransferEngine()
        cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=[1, 2, 3],
                             mode="download", threads=2, dedup=False, sid="abc")
        result = await asyncio.wait_for(eng.run(client, cfg), timeout=15)
        assert result.failed == 3 and result.success == 0, result
        print("download flood cap OK")
    finally:
        bconfig.config.MAX_FLOOD_SLEEP, bconfig.config.MAX_FLOOD_WAIT, bconfig.config.FLOOD_BUFFER = (
            old_sleep, old_cap, old_buf,
        )


async def test_download_progress_ticks():
    """Progress must keep updating (not look frozen) while a slow download is
    in flight."""
    src = FakeEntity(1)
    dst = FakeEntity(2)
    msgs = [_media_msg(1), _media_msg(2)]
    client = DownloadClient(msgs, delays={1: 1.2})
    eng = TransferEngine()
    cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=[1, 2],
                         mode="download", threads=2, dedup=False, sid="abc")

    ticks = []
    async def progress_cb(state):
        ticks.append(state["elapsed"])

    await asyncio.wait_for(eng.run(client, cfg, progress_cb), timeout=15)
    assert len(ticks) >= 3, ticks  # periodic ticks during the slow download
    print("download progress ticks OK")


async def test_pause_interrupts_download():
    """Pausing mid-download must interrupt the current transfer immediately
    (op cancellation) and Resume must retry the same item, losing no progress."""
    src = FakeEntity(1)
    dst = FakeEntity(2)
    msgs = [_media_msg(1), _media_msg(2)]
    client = DownloadClient(msgs, delays={1: 2.0})
    eng = TransferEngine()
    cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=[1, 2],
                         mode="download", threads=2, dedup=False, sid="abc")

    interrupted = {"downloads": 0}

    orig_dl = client.download_media

    async def tracking_download(msg, file=None, progress_callback=None):
        interrupted["downloads"] += 1
        return await orig_dl(msg, file=file, progress_callback=progress_callback)

    client.download_media = tracking_download

    async def pauser():
        await asyncio.sleep(0.3)
        eng.request_pause()
        await asyncio.sleep(0.4)
        eng.request_resume()

    pause_task = asyncio.create_task(pauser())
    result = await asyncio.wait_for(eng.run(client, cfg), timeout=15)
    await pause_task
    assert result.success == 2 and result.failed == 0, result
    # msg 1 was interrupted and restarted on resume (>=2 download attempts)
    assert interrupted["downloads"] >= 3, interrupted["downloads"]
    print("pause interrupts download + resume retries item OK")


async def test_item_retry_deadline_caps_unlimited():
    """With retry_count=0 (the UI's 'Unlimited'), a persistently failing item
    must still be abandoned once the per-item deadline is reached, so the
    queue always makes forward progress."""
    import bot.config as bconfig
    old_deadline = bconfig.config.MAX_ITEM_RETRY_SECONDS
    bconfig.config.MAX_ITEM_RETRY_SECONDS = 0.3
    try:
        src = FakeEntity(1)
        dst = FakeEntity(2)
        msgs = [FakeMessage(i, text=f"m{i}") for i in range(1, 4)]

        class FailingClient(FakeClient):
            async def forward_messages(self, dest, ids, from_peer=None, **kw):
                await asyncio.sleep(0.05)
                raise ConnectionError("boom")

        client = FailingClient(msgs)
        eng = TransferEngine()
        cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=[1, 2, 3],
                             mode="forward", options={"keep_sender"}, threads=1,
                             retry_count=0, auto_resume=True, dedup=False, sid="abc")
        result = await asyncio.wait_for(eng.run(client, cfg), timeout=15)
        assert result.total == 3 and result.success == 0 and result.failed == 0, result
        assert result.skipped == 3, result  # deadline abandons each item
        print("item retry deadline caps Unlimited retries OK")
    finally:
        bconfig.config.MAX_ITEM_RETRY_SECONDS = old_deadline


async def test_stall_watchdog_cancels_frozen_download():
    """A download that reports one progress tick and then stops must be
    cancelled by the stall watchdog (not hang forever) and count as failed."""
    import bot.config as bconfig
    old_stall = bconfig.config.STALL_TIMEOUT
    bconfig.config.STALL_TIMEOUT = 0.3
    try:
        src = FakeEntity(1)
        dst = FakeEntity(2)
        msgs = [_media_msg(1)]

        class StallingClient(FakeClient):
            async def download_media(self, msg, file=None, progress_callback=None):
                if progress_callback:
                    progress_callback(0, 100)
                await asyncio.sleep(60)  # frozen mid-download

        client = StallingClient(msgs)
        eng = TransferEngine()
        cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=[1],
                             mode="download", threads=2, retry_count=1,
                             dedup=False, sid="abc")
        result = await asyncio.wait_for(eng.run(client, cfg), timeout=15)
        assert result.success == 0 and result.failed == 1, result
        print("stall watchdog cancels frozen download OK")
    finally:
        bconfig.config.STALL_TIMEOUT = old_stall


async def test_fetch_failure_accounted_not_hung():
    """A chunk fetch that keeps failing must be counted as failed and the run
    must complete instead of hanging or crashing."""
    src = FakeEntity(1)
    dst = FakeEntity(2)
    msgs = [_media_msg(i) for i in range(1, 6)]

    class FlakyClient(FakeClient):
        async def get_messages(self, source, ids=None):
            raise ConnectionError("boom")

    client = FlakyClient(msgs)
    eng = TransferEngine()
    cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=[1, 2, 3, 4, 5],
                         mode="forward", options={"keep_sender"}, threads=1,
                         dedup=False, sid="abc")
    result = await asyncio.wait_for(eng.run(client, cfg), timeout=15)
    assert result.failed == 5 and result.success == 0, result
    print("fetch failure accounted, run completed OK")


async def test_watchdog_no_false_positive_during_collection():
    """Regression test for the production bug: a fresh worker must never be
    reported as 'stuck on item 0 for >900s' a few seconds after starting.

    The old watchdog computed ``time.monotonic() - self._item_deadline`` where
    ``_item_deadline`` was 0.0 until the first item began. On a host that had
    been up longer than MAX_ITEM_RETRY_SECONDS (the norm on Render), that
    expression was always > 900, so the very first watchdog check during
    message collection falsely aborted the run.

    The new watchdog anchors on ``_last_activity_ts`` which is always set from
    the same monotonic clock at job start and at every item start, so even on a
    host with a huge monotonic base value the elapsed time is ~0 while work is
    happening.
    """
    import bot.config as bconfig
    import bot.transfer_engine as te

    real_mono = time.monotonic
    base = 1_000_000.0  # host up for ~11.5 days

    # A pure offset keeps time deltas identical, so asyncio's own timers still
    # work while the engine sees a large monotonic base value.
    def fake_mono():
        return base + real_mono()

    old_limit = bconfig.config.MAX_ITEM_RETRY_SECONDS
    old_interval = bconfig.config.WATCHDOG_INTERVAL
    te.time.monotonic = fake_mono
    try:
        bconfig.config.MAX_ITEM_RETRY_SECONDS = 900
        bconfig.config.WATCHDOG_INTERVAL = 0.02  # aggressive watchdog checks

        src = FakeEntity(1)
        dst = FakeEntity(2)
        msgs = [FakeMessage(i, text=f"m{i}") for i in range(1, 51)]

        class SlowFetchClient(FakeClient):
            async def get_messages(self, source, ids=None):
                # Collection outlives several watchdog wake-ups, so the first
                # check fires while item 0 has not even started.
                await asyncio.sleep(0.05)
                return await super().get_messages(source, ids=ids)

        client = SlowFetchClient(msgs)
        eng = TransferEngine()
        cfg = TransferConfig(source_entity=src, dest_entity=dst,
                             message_ids=list(range(1, 51)), mode="forward",
                             threads=5, dedup=False, sid="abc")
        result = await asyncio.wait_for(eng.run(client, cfg), timeout=15)
        assert not result.cancelled, result
        assert result.success == 50, result
        print("watchdog no false positive during collection OK")
    finally:
        te.time.monotonic = real_mono
        bconfig.config.MAX_ITEM_RETRY_SECONDS = old_limit
        bconfig.config.WATCHDOG_INTERVAL = old_interval


async def test_watchdog_fires_on_real_stall():
    """A run that genuinely stops making forward progress must still be aborted
    by the run-level watchdog (it must not be permanently disabled)."""
    import bot.config as bconfig

    old_limit = bconfig.config.MAX_ITEM_RETRY_SECONDS
    old_interval = bconfig.config.WATCHDOG_INTERVAL
    bconfig.config.MAX_ITEM_RETRY_SECONDS = 0.2
    bconfig.config.WATCHDOG_INTERVAL = 0.05
    try:
        eng = TransferEngine()
        eng._last_activity_ts = time.monotonic() - 5.0  # genuinely stalled
        task = asyncio.create_task(eng._run_watchdog())
        await asyncio.wait_for(task, timeout=3)
        assert eng._stop.is_set(), "watchdog should have fired on a real stall"
        print("watchdog fires on real stall OK")
    finally:
        bconfig.config.MAX_ITEM_RETRY_SECONDS = old_limit
        bconfig.config.WATCHDOG_INTERVAL = old_interval


async def test_watchdog_ignores_paused_run():
    """A deliberately paused run is not a stall: the watchdog must never kill it."""
    import bot.config as bconfig

    old_limit = bconfig.config.MAX_ITEM_RETRY_SECONDS
    old_interval = bconfig.config.WATCHDOG_INTERVAL
    bconfig.config.MAX_ITEM_RETRY_SECONDS = 0.2
    bconfig.config.WATCHDOG_INTERVAL = 0.05
    try:
        eng = TransferEngine()
        eng._last_activity_ts = time.monotonic() - 5.0
        eng._paused = True
        task = asyncio.create_task(eng._run_watchdog())
        try:
            await asyncio.sleep(0.3)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert not eng._stop.is_set(), "watchdog must not kill a paused run"
        print("watchdog ignores paused run OK")
    finally:
        bconfig.config.MAX_ITEM_RETRY_SECONDS = old_limit
        bconfig.config.WATCHDOG_INTERVAL = old_interval


async def test_stall_watchdog_catches_no_first_progress():
    """A download that never reports its first progress tick (e.g. a part-path
    request wedging before the first byte) must still be cancelled by the stall
    watchdog instead of hanging forever.

    This guards the progress-seeding fix in _download_one: the download slot is
    seeded to done=0 so _guard arms its stall watchdog from the very first poll
    even before the first progress_callback fires.
    """
    import bot.config as bconfig

    old_stall = bconfig.config.STALL_TIMEOUT
    bconfig.config.STALL_TIMEOUT = 0.3
    try:
        src = FakeEntity(1)
        dst = FakeEntity(2)
        msgs = [_media_msg(1)]

        class SilentDownloadClient(FakeClient):
            async def download_media(self, msg, file=None, progress_callback=None):
                await asyncio.sleep(60)  # never reports progress

        client = SilentDownloadClient(msgs)
        eng = TransferEngine()
        cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=[1],
                             mode="download", threads=1, retry_count=1,
                             dedup=False, sid="abc")
        result = await asyncio.wait_for(eng.run(client, cfg), timeout=15)
        assert result.success == 0 and result.failed == 1, result
        print("stall watchdog catches no-first-progress download OK")
    finally:
        bconfig.config.STALL_TIMEOUT = old_stall


def test_progress_slot_orphan_cannot_clobber():
    # A late callback from a cancelled/orphaned op must never replace the
    # current op's snapshot nor refresh the run watchdog anchor (which would
    # mask a genuine stall).
    eng = TransferEngine()
    cb1, slot1 = eng._file_progress_cb("a.txt", "Downloading", "dl")
    eng._file_dl = slot1
    cb1(100, 1000)  # current op publishes normally
    assert eng._file_dl is slot1 and slot1["done"] == 100
    anchor = eng._last_activity_ts

    cb2, slot2 = eng._file_progress_cb("b.txt", "Downloading", "dl")
    eng._file_dl = slot2  # the next op replaces the slot
    cb1(500, 1000)  # orphan callback fires late
    assert eng._file_dl is slot2, "orphan must not replace the current slot"
    assert slot2["done"] == 0, "orphan must not write into the current slot"
    assert eng._last_activity_ts == anchor, "orphan must not refresh the watchdog anchor"

    cb2(10, 1000)
    assert slot2["done"] == 10
    assert eng._file_dl is slot2
    print("progress slot orphan isolation OK")


async def test_guard_stubborn_task_raises_control_exception():
    import bot.transfer_engine as te

    old_wait = te._CANCEL_WAIT
    te._CANCEL_WAIT = 0.05
    try:
        eng = TransferEngine()

        async def stubborn():
            # Swallows the first CancelledError and keeps running, simulating a
            # Telethon op wedged in a long synchronous write that ignores
            # cancellation for longer than _CANCEL_WAIT.
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(0.1)

        async def stopper():
            await asyncio.sleep(0.05)
            eng.request_stop()

        stopper_task = asyncio.create_task(stopper())
        try:
            start = time.monotonic()
            try:
                await eng._guard(stubborn(), opname="stubborn")
                raise AssertionError("expected _Stopped")
            except te._Stopped:
                pass
            elapsed = time.monotonic() - start
            assert elapsed < 2.0, f"_guard cleanup must not hang (took {elapsed:.1f}s)"
        finally:
            stopper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopper_task
            # nothing may leak into the next run
            assert not eng._op_tasks, eng._op_tasks
    finally:
        te._CANCEL_WAIT = old_wait
    print("guard stubborn-task control exception OK")


async def test_dedup_and_mark_db_failure_do_not_abort_run():
    from bot.transfer_engine import db as engine_db

    src, dst = FakeEntity(1), FakeEntity(2)
    msgs = [FakeMessage(10, text="hi")]
    client = FakeClient(msgs)
    eng = TransferEngine()
    cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=[10],
                         mode="forward", dedup=True, sid="abc")

    async def boom(*args, **kwargs):
        raise asyncio.TimeoutError("mongo unavailable")

    old_is = engine_db.is_transferred
    old_mark = engine_db.mark_transferred
    engine_db.is_transferred = boom
    engine_db.mark_transferred = boom
    try:
        result = await eng.run(client, cfg)
    finally:
        engine_db.is_transferred = old_is
        engine_db.mark_transferred = old_mark

    assert result.success == 1 and result.failed == 0 and not result.cancelled, result
    print("dedup/mark DB failure tolerance OK")


async def main():
    test_parse_input()
    test_filter()
    test_album_grouping()
    await test_run_copy_forward()
    await test_dedup_skip()
    await test_stop_no_hang()
    await test_forward_strict_order()
    await test_download_ordered_pipeline()
    await test_download_mixed_order()
    await test_download_strict_serial()
    await test_download_pipeline_stop()
    await test_download_flood_cap()
    await test_download_progress_ticks()
    await test_pause_interrupts_download()
    await test_item_retry_deadline_caps_unlimited()
    await test_stall_watchdog_cancels_frozen_download()
    await test_fetch_failure_accounted_not_hung()
    await test_watchdog_no_false_positive_during_collection()
    await test_watchdog_fires_on_real_stall()
    await test_watchdog_ignores_paused_run()
    await test_stall_watchdog_catches_no_first_progress()
    test_progress_slot_orphan_cannot_clobber()
    await test_guard_stubborn_task_raises_control_exception()
    await test_dedup_and_mark_db_failure_do_not_abort_run()
    print("ALL TESTS PASSED")


asyncio.run(main())
