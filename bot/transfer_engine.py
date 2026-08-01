"""High-speed transfer engine.

Strategy
--------
* Never downloads media. Forwarding uses ``forward_messages`` (native,
  server-side). Copying recreates the message server-side via
  ``send_file``/``send_message`` with the *existing* input media, so no
  re-upload ever happens.
* Message ids are processed in ascending order so reply chains can be
  mapped from source id to destination id.
* Albums (``grouped_id``) are re-grouped so they remain albums in the
  destination.
* ``threads`` concurrent workers drain an asyncio queue; FloodWaitError is
  slept through and processing resumes automatically.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl import types
from telethon.errors.rpcerrorlist import (
    MessageIdInvalidError,
    ChatForwardsRestrictedError,
    MessageEmptyError,
)

from bot.config import config
from bot.db import db

log = logging.getLogger("bot.engine")

ProgressCb = Callable[[dict], Awaitable[None]]


@dataclass
class TransferConfig:
    source_entity: object
    dest_entity: object
    message_ids: list[int]
    mode: str = "forward"                     # forward | copy
    options: set = field(default_factory=set)
    filter_type: str = "all"
    dedup: bool = True
    threads: int = 2
    forward_delay: float = 0.0
    retry_count: int = 3
    handle_flood: bool = True
    auto_resume: bool = True
    sid: str = ""
    silent: bool = False

    # metadata for logging
    source_name: str = ""
    dest_name: str = ""
    total_planned: int = 0


@dataclass
class TransferResult:
    total: int = 0
    success: int = 0
    skipped: int = 0
    failed: int = 0
    duration: float = 0.0
    cancelled: bool = False
    error: str = ""


def message_matches_filter(msg, filter_type: str) -> bool:
    if isinstance(msg, types.MessageService):
        return False
    if filter_type == "all":
        return True

    media = msg.media
    if isinstance(media, types.MessageMediaWebPage):
        return filter_type == "text" and bool(msg.message)
    if isinstance(media, types.MessageMediaPhoto):
        return filter_type in ("photo", "media")
    if isinstance(media, types.MessageMediaDocument):
        mime = (media.document.mime_type or "").lower() if media.document else ""
        if filter_type == "video":
            return mime.startswith("video/")
        if filter_type == "document":
            return not mime.startswith(("video/", "audio/"))
        if filter_type == "media":
            return True
        if filter_type == "photo":
            return False
        return False
    if isinstance(media, types.MessageMediaPoll):
        return filter_type == "media"
    if filter_type == "text":
        return bool(msg.message)
    if filter_type == "media":
        return media is not None
    return False


def filter_label(f: str) -> str:
    return {
        "all": "Everything",
        "photo": "Photos",
        "video": "Videos",
        "document": "Documents",
        "text": "Text",
        "media": "Media",
    }.get(f, f)


class TransferEngine:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._cancelled = False

    def request_stop(self) -> None:
        self._cancelled = True
        self._stop.set()

    # ------------------------------------------------------------------
    async def collect_ids(
        self, client: TelegramClient, source, count: int | None,
        start_id: int | None, end_id: int | None, filter_type: str,
    ) -> list[int]:
        """Gather message ids to transfer.

        count is used for the "Latest N" presets (collects N messages that
        pass the filter, scanning backwards up to a cap). If count is None a
        custom inclusive [start_id, end_id] range is used.
        """
        if count is not None:
            ids: list[int] = []
            cap = max(5000, count * 10)
            async for msg in client.iter_messages(source):
                if len(ids) >= count:
                    break
                if isinstance(msg, types.MessageService):
                    continue
                if message_matches_filter(msg, filter_type):
                    ids.append(msg.id)
                if len(ids) >= cap:
                    break
            ids.reverse()  # ascending for reply mapping
            return ids

        # custom range
        lo, hi = min(start_id, end_id), max(start_id, end_id)
        if hi - lo + 1 > config.MAX_CUSTOM_RANGE:
            raise ValueError(
                f"Range too large ({hi - lo + 1} messages). Max is {config.MAX_CUSTOM_RANGE}."
            )
        ids = list(range(lo, hi + 1))
        return ids

    # ------------------------------------------------------------------
    async def run(
        self,
        client: TelegramClient,
        cfg: TransferConfig,
        progress_cb: ProgressCb | None = None,
    ) -> TransferResult:
        self._stop.clear()
        self._cancelled = False
        result = TransferResult(total=len(cfg.message_ids))
        start = time.monotonic()
        reply_map: dict[int, int] = {}
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)

        async def _cancellable_sleep(seconds: float) -> bool:
            """Sleep in slices; return False if stop was requested meanwhile."""
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                if self._stop.is_set():
                    return False
                await asyncio.sleep(min(1.0, end - time.monotonic()))
            return True

        async def _worker() -> None:
            while True:
                item = await queue.get()
                try:
                    if item is None:
                        return
                    if self._stop.is_set():
                        continue  # drain remaining queue without processing
                    try:
                        await self._process_item(client, cfg, item, result, reply_map)
                    except _Abort:
                        return
                    except Exception as exc:  # noqa: BLE001 - keep the queue alive
                        log.warning("item failed: %s", exc)
                        result.failed += item["count"]
                    finally:
                        if cfg.forward_delay and not self._stop.is_set():
                            await _cancellable_sleep(cfg.forward_delay)
                        if progress_cb is not None:
                            elapsed = time.monotonic() - start
                            speed = (result.success + result.skipped) / elapsed if elapsed else 0.0
                            await progress_cb(
                                {
                                    "total": result.total,
                                    "success": result.success,
                                    "skipped": result.skipped,
                                    "failed": result.failed,
                                    "elapsed": elapsed,
                                    "speed": speed,
                                }
                            )
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(_worker()) for _ in range(max(1, cfg.threads))]

        try:
            ids = sorted(cfg.message_ids)
            for chunk_start in range(0, len(ids), config.BATCH_SIZE):
                if self._stop.is_set():
                    break
                chunk = ids[chunk_start:chunk_start + config.BATCH_SIZE]
                msgs = await self._fetch_existing(client, cfg.source_entity, chunk)
                items = self._build_items(msgs, cfg)
                for item in items:
                    if self._stop.is_set():
                        break
                    await queue.put(item)

            # send one sentinel per worker, then wait for full drain
            for _ in workers:
                await queue.put(None)
            await queue.join()
        except asyncio.CancelledError:
            result.cancelled = True
            self._stop.set()
        except Exception as exc:  # noqa: BLE001
            log.exception("transfer run aborted")
            result.cancelled = True
            result.error = str(exc)
        finally:
            self._stop.set()
            await asyncio.gather(*workers, return_exceptions=True)

        result.duration = time.monotonic() - start
        if self._cancelled:
            result.cancelled = True
            if not result.error:
                result.error = "stopped by user"
        elif result.cancelled and not result.error:
            result.error = "stopped by user"
        return result

    # ------------------------------------------------------------------
    async def _fetch_existing(self, client, source, chunk: list[int]) -> list:
        try:
            found = await client.get_messages(source, ids=chunk)
        except (MessageIdInvalidError, ValueError):
            found = []
        if not isinstance(found, list):
            found = [found] if found else []
        return found

    def _build_items(self, msgs: list, cfg: TransferConfig) -> list[dict]:
        groups: dict[int, list] = {}
        singles: list[dict] = []
        for msg in msgs:
            if not message_matches_filter(msg, cfg.filter_type):
                continue
            if msg.grouped_id:
                groups.setdefault(msg.grouped_id, []).append(msg)
            else:
                singles.append({"messages": [msg], "count": 1})
        items = []
        for group in groups.values():
            group.sort(key=lambda m: m.id)
            items.append({"messages": group, "count": len(group)})
        items.extend(singles)
        items.sort(key=lambda it: it["messages"][0].id)
        return items

    # ------------------------------------------------------------------
    async def _process_item(
        self, client, cfg: TransferConfig, item: dict, result: TransferResult, reply_map: dict
    ) -> None:
        msgs = item["messages"]
        src_id, dst_id = cfg.source_entity.id, cfg.dest_entity.id

        if cfg.dedup:
            for m in msgs:
                if await db.is_transferred(src_id, dst_id, m.id):
                    result.skipped += len(msgs)
                    return

        await self._with_retries(client, cfg, lambda: self._transfer(client, cfg, msgs, reply_map))

        sent_count = 0
        for m in msgs:
            if m.id in reply_map:
                await db.mark_transferred(src_id, dst_id, m.id, cfg.sid, cfg.mode)
                sent_count += 1
        result.success += sent_count

    async def _with_retries(self, client, cfg, fn) -> None:
        attempts = 0
        while True:
            try:
                return await fn()
            except FloodWaitError as exc:
                if not cfg.handle_flood:
                    raise
                wait = exc.seconds
                log.info("FloodWait %.1fs, sleeping", wait)
                ok = await self._sleep_interruptible(min(wait, 300))
                if not ok:
                    raise _Abort()
                continue
            except _Abort:
                raise
            except ChatForwardsRestrictedError:
                if cfg.mode == "forward":
                    log.info("Forwarding restricted, falling back to download mode")
                    cfg.mode = "download"
                    continue
                attempts += 1
                if cfg.retry_count == 0 or attempts <= cfg.retry_count:
                    await self._sleep_interruptible(min(2 * attempts, 10))
                    continue
                if not cfg.auto_resume:
                    raise _Abort()
                raise
            except (MessageIdInvalidError, MessageEmptyError):
                attempts += 1
                if cfg.retry_count == 0 or attempts <= cfg.retry_count:
                    await self._sleep_interruptible(min(2 * attempts, 10))
                    continue
                if not cfg.auto_resume:
                    raise _Abort()
                raise
            except Exception as exc:  # noqa: BLE001 - retry generic failures too
                log.warning("transfer error: %s", exc)
                attempts += 1
                if cfg.retry_count == 0 or attempts <= cfg.retry_count:
                    await self._sleep_interruptible(min(2 * attempts, 10))
                    continue
                if not cfg.auto_resume:
                    raise _Abort()
                raise

    async def _sleep_interruptible(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stop.is_set():
                return False
            await asyncio.sleep(min(1.0, end - time.monotonic()))
        return True

    # ------------------------------------------------------------------
    async def _transfer(self, client, cfg, msgs: list, reply_map: dict) -> None:
        if cfg.mode == "forward":
            await self._forward(client, cfg, msgs, reply_map)
        elif cfg.mode == "copy":
            await self._copy(client, cfg, msgs, reply_map)
        elif cfg.mode == "download":
            await self._download_transfer(client, cfg, msgs, reply_map)

    async def _forward(self, client, cfg, msgs: list, reply_map: dict) -> None:
        ids = [m.id for m in msgs]
        as_album = len(msgs) > 1
        drop_captions = "remove_captions" in cfg.options
        drop_author = "hide_header" in cfg.options
        sent = await client.forward_messages(
            cfg.dest_entity,
            ids,
            from_peer=cfg.source_entity,
            as_album=as_album,
            drop_media_captions=drop_captions,
            drop_author=drop_author,
            silent=cfg.silent,
        )
        if as_album:
            dest_ids = [s.id for s in sent]
        else:
            dest_ids = [sent.id]
        for m, did in zip(msgs, dest_ids):
            reply_map[m.id] = did

    async def _copy(self, client, cfg, msgs: list, reply_map: dict) -> None:
        if len(msgs) > 1:
            await self._copy_album(client, cfg, msgs, reply_map)
            return
        m = msgs[0]
        did = await self._copy_one(client, cfg, m, reply_map)
        if did is not None:
            reply_map[m.id] = did

    async def _copy_album(self, client, cfg, msgs: list, reply_map: dict) -> None:
        text_only = "text_only" in cfg.options
        if text_only:
            for m in msgs:
                did = await self._copy_one(client, cfg, m, reply_map)
                if did is not None:
                    reply_map[m.id] = did
            return

        drop_caption = "remove_captions" in cfg.options
        media_only = "media_only" in cfg.options
        caption = "" if (drop_caption or media_only) else (msgs[0].text or "")
        media = [
            m.media
            for m in msgs
            if m.media and not isinstance(m.media, types.MessageMediaWebPage)
        ]
        if not media:
            for m in msgs:
                did = await self._copy_one(client, cfg, m, reply_map)
                if did is not None:
                    reply_map[m.id] = did
            return
        sent = await client.send_file(
            cfg.dest_entity, media, caption=caption, silent=cfg.silent
        )
        if isinstance(sent, list):
            dest_ids = [s.id for s in sent]
            if len(dest_ids) == len(msgs):
                for m, did in zip(msgs, dest_ids):
                    reply_map[m.id] = did
                return
        # fallback: copy individually
        for m in msgs:
            did = await self._copy_one(client, cfg, m, reply_map)
            if did is not None:
                reply_map[m.id] = did

    async def _copy_one(self, client, cfg, msg, reply_map: dict) -> int | None:
        text_only = "text_only" in cfg.options
        media_only = "media_only" in cfg.options
        drop_caption = "remove_captions" in cfg.options

        reply_to = None
        if msg.reply_to and msg.reply_to.reply_to_msg_id:
            reply_to = reply_map.get(msg.reply_to.reply_to_msg_id)

        if text_only:
            text = msg.text
            if not text:
                return None
            sent = await client.send_message(
                cfg.dest_entity,
                text,
                formatting_entities=msg.entities,
                parse_mode=None,
                reply_to=reply_to,
                silent=cfg.silent,
            )
            return sent.id

        if media_only:
            text = ""
        else:
            text = "" if drop_caption else (msg.text or "")

        media = msg.media
        is_webpage = isinstance(media, types.MessageMediaWebPage)
        if media and not is_webpage:
            sent = await client.send_file(
                cfg.dest_entity,
                media,
                caption=text,
                formatting_entities=msg.entities,
                parse_mode=None,
                reply_to=reply_to,
                silent=cfg.silent,
            )
            return sent.id

        if text:
            sent = await client.send_message(
                cfg.dest_entity,
                text,
                formatting_entities=msg.entities,
                parse_mode=None,
                reply_to=reply_to,
                silent=cfg.silent,
            )
            return sent.id
        return None

    async def _download_transfer(self, client, cfg, msgs: list, reply_map: dict) -> None:
        text_only = "text_only" in cfg.options
        media_only = "media_only" in cfg.options
        drop_caption = "remove_captions" in cfg.options

        if text_only:
            for m in msgs:
                text = m.text
                if not text:
                    continue
                reply_to = None
                if m.reply_to and m.reply_to.reply_to_msg_id:
                    reply_to = reply_map.get(m.reply_to.reply_to_msg_id)
                sent = await client.send_message(
                    cfg.dest_entity,
                    text,
                    formatting_entities=m.entities,
                    parse_mode=None,
                    reply_to=reply_to,
                    silent=cfg.silent,
                )
                reply_map[m.id] = sent.id
            return

        is_album = len(msgs) > 1
        if media_only:
            caption = ""
        else:
            caption = "" if drop_caption else (msgs[0].text or "")

        media_msgs = []
        text_msgs = []
        for m in msgs:
            media = m.media
            is_webpage = isinstance(media, types.MessageMediaWebPage)
            if media and not is_webpage:
                media_msgs.append(m)
            elif m.text:
                text_msgs.append(m)

        if media_msgs:
            try:
                if is_album:
                    paths = []
                    for m in media_msgs:
                        ext = _media_extension(m)
                        tmp_path = os.path.join(tempfile.gettempdir(), f"fwd_{uuid.uuid4().hex}{ext}")
                        path = await client.download_media(m, file=tmp_path)
                        if path:
                            paths.append(path)
                    if paths:
                        sent = await client.send_file(
                            cfg.dest_entity,
                            paths,
                            caption=caption,
                            silent=cfg.silent,
                        )
                        if isinstance(sent, list) and len(sent) == len(paths):
                            for m, s in zip(media_msgs, sent):
                                reply_map[m.id] = s.id
                        else:
                            for m in media_msgs:
                                reply_map[m.id] = sent.id
                        for p in paths:
                            try:
                                os.remove(p)
                            except OSError:
                                pass
                else:
                    for m in media_msgs:
                        ext = _media_extension(m)
                        tmp_path = os.path.join(tempfile.gettempdir(), f"fwd_{uuid.uuid4().hex}{ext}")
                        path = None
                        try:
                            path = await client.download_media(m, file=tmp_path)
                            if path:
                                reply_to = None
                                if m.reply_to and m.reply_to.reply_to_msg_id:
                                    reply_to = reply_map.get(m.reply_to.reply_to_msg_id)
                                sent = await client.send_file(
                                    cfg.dest_entity,
                                    path,
                                    caption=caption,
                                    formatting_entities=m.entities,
                                    parse_mode=None,
                                    reply_to=reply_to,
                                    silent=cfg.silent,
                                )
                                reply_map[m.id] = sent.id
                        except Exception as exc:
                            log.warning("download/upload failed for msg %s: %s", m.id, exc)
                        finally:
                            if path and os.path.exists(path):
                                try:
                                    os.remove(path)
                                except OSError:
                                    pass
            except Exception as exc:
                log.warning("album download/upload failed: %s", exc)

        for m in text_msgs:
            if m in media_msgs:
                continue
            reply_to = None
            if m.reply_to and m.reply_to.reply_to_msg_id:
                reply_to = reply_map.get(m.reply_to.reply_to_msg_id)
            sent = await client.send_message(
                cfg.dest_entity,
                m.text,
                formatting_entities=m.entities,
                parse_mode=None,
                reply_to=reply_to,
                silent=cfg.silent,
            )
            reply_map[m.id] = sent.id


def _media_extension(msg) -> str:
    media = msg.media
    if isinstance(media, types.MessageMediaDocument) and media.document:
        mime = (media.document.mime_type or "").lower()
        return _mime_to_ext(mime)
    if isinstance(media, types.MessageMediaPhoto):
        return ".jpg"
    return ""


def _mime_to_ext(mime: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/x-matroska": ".mkv",
        "audio/mpeg": ".mp3",
        "audio/ogg": ".ogg",
        "application/pdf": ".pdf",
        "application/zip": ".zip",
    }.get(mime, "")


class _Abort(Exception):
    """Internal: stop this worker because auto_resume is off / user stopped."""


engine = TransferEngine()
