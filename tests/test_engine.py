"""Standalone smoke tests (no live Telegram API needed)."""
import asyncio
import os
import tempfile

os.environ.setdefault("API_ID", "0")
os.environ.setdefault("API_HASH", "x")
os.environ.setdefault("BOT_TOKEN", "x")

from telethon.tl import types  # noqa: E402

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
    def __init__(self, messages, delays=None):
        super().__init__(messages)
        self.delays = delays or {}
        self.upload_order = []

    async def download_media(self, msg, file=None):
        if self.delays.get(msg.id):
            await asyncio.sleep(self.delays[msg.id])
        with open(file, "w") as f:
            f.write(str(msg.id))
        return file

    async def send_file(self, dest, file, **kw):
        if isinstance(file, list):
            ids = []
            for p in file:
                with open(p) as f:
                    ids.append(int(f.read()))
                self.upload_order.append(ids[-1])
            sent = [FakeMessage(8000 + i) for i in range(len(ids))]
            self.sent.append(("dl_album", ids, kw))
            return sent
        with open(file) as f:
            mid = int(f.read())
        self.upload_order.append(mid)
        self.sent.append(("dl_file", mid, kw))
        return FakeMessage(9000 + mid)

    async def send_message(self, dest, text, **kw):
        self.sent.append(("dl_text", text, kw))
        try:
            self.upload_order.append(int(str(text).strip()))
        except ValueError:
            self.upload_order.append(-1)
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


async def test_download_pipeline_stop():
    src = FakeEntity(1)
    dst = FakeEntity(2)
    msgs = [_media_msg(i) for i in range(1, 60)]
    client = DownloadClient(msgs, delays={i: 0.02 for i in range(1, 60)})
    eng = TransferEngine()

    async def stopper():
        await asyncio.sleep(0.15)
        eng.request_stop()

    cfg = TransferConfig(source_entity=src, dest_entity=dst, message_ids=list(range(1, 60)),
                         mode="download", threads=4, dedup=False, sid="abc")
    stop_task = asyncio.create_task(stopper())
    result = await asyncio.wait_for(eng.run(client, cfg), timeout=10)
    await stop_task
    assert result.cancelled, result
    print(f"download pipeline stop OK (uploaded {len(client.upload_order)})")


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
    await test_download_pipeline_stop()
    print("ALL TESTS PASSED")


asyncio.run(main())
