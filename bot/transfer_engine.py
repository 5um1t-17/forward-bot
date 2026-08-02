"""High-speed transfer engine.

Strategy
--------
* Never downloads media in Forward/Copy mode. Forwarding uses
  ``forward_messages`` (native, server-side). Copying recreates the message
  server-side via ``send_file``/``send_message`` with the *existing* input
  media, so no re-upload ever happens.
* **Strict ordering**: forward/copy/dedup items are processed one at a time in
  source order, so the destination always mirrors the source exactly.
* **Download & Re-upload** mode (for restricted private groups) uses an
  order-preserving high-speed pipeline: up to ``threads`` downloads run in
  parallel, and file bytes are pre-uploaded in parallel too (``upload_file``),
  while the final send is committed strictly in source order — so the heavy
  transfer is fully pipelined yet the destination always mirrors the source.
* Message ids are processed in ascending order so reply chains can be
  mapped from source id to destination id.
* Albums (``grouped_id``) are re-grouped so they remain albums in the
  destination.
* FloodWaitError is slept through and processing resumes automatically.
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

        if cfg.mode == "download":
            # Order-preserving high-speed path: several downloads run in
            # parallel (window = threads) while uploads are committed strictly
            # in source order, so the destination mirrors the source no matter
            # how file sizes differ.
            try:
                await self._run_download_pipeline(client, cfg, result, progress_cb, start)
            except asyncio.CancelledError:
                result.cancelled = True
                self._stop.set()
            except Exception as exc:  # noqa: BLE001
                log.exception("download transfer aborted")
                result.cancelled = True
                result.error = str(exc)
            finally:
                self._stop.set()
            result.duration = time.monotonic() - start
            if self._cancelled:
                result.cancelled = True
                if not result.error:
                    result.error = "stopped by user"
            elif result.cancelled and not result.error:
                result.error = "stopped by user"
            return result

        reply_map: dict[int, int] = {}
        ids = sorted(cfg.message_ids)

        async def _cancellable_sleep(seconds: float) -> bool:
            """Sleep in slices; return False if stop was requested meanwhile."""
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                if self._stop.is_set():
                    return False
                await asyncio.sleep(min(1.0, end - time.monotonic()))
            return True

        aborted = False
        try:
            for chunk_start in range(0, len(ids), config.BATCH_SIZE):
                if self._stop.is_set():
                    break
                chunk = ids[chunk_start:chunk_start + config.BATCH_SIZE]
                msgs = await self._fetch_existing(client, cfg.source_entity, chunk)
                items = self._build_items(msgs, cfg)
                for item in items:
                    if self._stop.is_set():
                        break
                    try:
                        await self._process_item(client, cfg, item, result, reply_map)
                    except _Abort:
                        aborted = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        log.warning("item failed: %s", exc)
                        result.failed += item["count"]
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
        except asyncio.CancelledError:
            result.cancelled = True
            self._stop.set()
        except Exception as exc:  # noqa: BLE001
            log.exception("transfer run aborted")
            result.cancelled = True
            result.error = str(exc)
        finally:
            self._stop.set()

        result.duration = time.monotonic() - start
        if self._cancelled:
            result.cancelled = True
            if not result.error:
                result.error = "stopped by user"
        elif aborted and not result.error:
            result.cancelled = True
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
    # Order-preserving high-speed download / re-upload pipeline.
    #
    # Downloads are dispatched up to ``cfg.threads`` at a time (so big files
    # overlap with the upload of the previous message), while uploads are
    # committed strictly in source order. The destination therefore mirrors
    # the source exactly, regardless of individual file sizes.
    # ------------------------------------------------------------------
    async def _run_download_pipeline(
        self, client, cfg: TransferConfig, result: TransferResult,
        progress_cb: ProgressCb | None, start: float,
    ) -> None:
        ids = sorted(cfg.message_ids)
        items: list[dict] = []
        for chunk_start in range(0, len(ids), config.BATCH_SIZE):
            if self._stop.is_set():
                break
            chunk = ids[chunk_start:chunk_start + config.BATCH_SIZE]
            msgs = await self._fetch_existing(client, cfg.source_entity, chunk)
            items.extend(self._build_items(msgs, cfg))

        reply_map: dict[int, int] = {}
        window = max(1, min(cfg.threads, config.MAX_THREADS))
        sem = asyncio.Semaphore(window)
        up_sem = asyncio.Semaphore(window)
        ready: asyncio.Queue = asyncio.Queue()
        temp_paths: set[str] = set()
        up_tasks: set[asyncio.Task] = set()

        async def downloader(idx: int, item: dict) -> None:
            msgs = item["messages"]
            src_id, dst_id = cfg.source_entity.id, cfg.dest_entity.id
            try:
                async with sem:
                    if self._stop.is_set():
                        await ready.put((idx, None, "cancelled"))
                        return
                    if cfg.dedup:
                        for m in msgs:
                            if await db.is_transferred(src_id, dst_id, m.id):
                                result.skipped += len(msgs)
                                await ready.put((idx, None, "skipped"))
                                return
                    pkg = await self._with_retries(
                        client, cfg,
                        lambda: self._download_item(client, cfg, msgs, temp_paths),
                    )
                if pkg is not None:
                    # hand off to a dedicated uploader task so the download
                    # slot is freed and uploads overlap with further downloads
                    task = asyncio.create_task(uploader(idx, pkg))
                    up_tasks.add(task)
                    task.add_done_callback(up_tasks.discard)
                else:
                    await ready.put((idx, None, "ok"))
            except _Abort:
                await ready.put((idx, None, "abort"))
            except asyncio.CancelledError:
                await ready.put((idx, None, "cancelled"))
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("download failed for group starting msg %s: %s", msgs[0].id, exc)
                result.failed += len(msgs)
                await ready.put((idx, None, "failed"))

        async def uploader(idx: int, pkg: dict) -> None:
            # Upload file bytes in parallel (bounded). The actual send stays
            # strictly ordered in the commit loop below.
            try:
                async with up_sem:
                    if pkg["kind"] == "media":
                        for payload in pkg["payloads"]:
                            path = payload.get("path")
                            if path and "file" not in payload:
                                payload["file"] = await self._with_retries(
                                    client, cfg, lambda p=path: client.upload_file(p)
                                )
                await ready.put((idx, pkg, "ok"))
            except _Abort:
                await ready.put((idx, None, "abort"))
            except asyncio.CancelledError:
                await ready.put((idx, None, "cancelled"))
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("file upload failed for group starting msg %s: %s", pkg["msgs"][0].id, exc)
                result.failed += len(pkg["msgs"])
                await ready.put((idx, None, "failed"))

        def cleanup_temp() -> None:
            for path in list(temp_paths):
                try:
                    os.remove(path)
                except OSError:
                    pass
            temp_paths.clear()

        tasks = [asyncio.create_task(downloader(i, item)) for i, item in enumerate(items)]

        next_seq = 0
        pending: dict[int, tuple] = {}
        remaining = len(items)
        abort = False
        try:
            while remaining and not abort:
                if self._stop.is_set():
                    abort = True
                    break
                try:
                    idx, pkg, status = await asyncio.wait_for(ready.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    if all(t.done() for t in tasks) and not up_tasks and ready.empty():
                        break
                    continue
                pending[idx] = (pkg, status)
                # commit in strict source order
                while next_seq in pending:
                    pkg, status = pending.pop(next_seq)
                    next_seq += 1
                    remaining -= 1
                    if status == "ok" and pkg is not None and not self._stop.is_set():
                        try:
                            await self._with_retries(
                                client, cfg,
                                lambda: self._upload_item(client, cfg, pkg, reply_map),
                            )
                        except _Abort:
                            abort = True
                            break
                        except Exception as exc:  # noqa: BLE001
                            log.warning("upload failed for group: %s", exc)
                            result.failed += len(pkg["msgs"])
                        else:
                            sent = 0
                            for m in pkg["msgs"]:
                                if m.id in reply_map:
                                    await db.mark_transferred(
                                        cfg.source_entity.id, cfg.dest_entity.id,
                                        m.id, cfg.sid, cfg.mode,
                                    )
                                    sent += 1
                            result.success += sent
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
            for task in list(tasks) + list(up_tasks):
                task.cancel()
            await asyncio.gather(*(list(tasks) + list(up_tasks)), return_exceptions=True)
            cleanup_temp()

    async def _download_item(
        self, client, cfg: TransferConfig, msgs: list, temp_paths: set[str],
    ) -> dict | None:
        """Download an item (single message or album) to temp files.

        Returns a package dict for :meth:`_upload_item`, or None if there is
        nothing uploadable.
        """
        text_only = "text_only" in cfg.options
        media_only = "media_only" in cfg.options
        drop_caption = "remove_captions" in cfg.options

        if text_only:
            payloads = [{"msg": m, "path": None, "text": m.text} for m in msgs if m.text]
            if not payloads:
                return None
            return {"msgs": msgs, "kind": "text", "payloads": payloads, "caption": ""}

        caption = "" if (drop_caption or media_only) else (msgs[0].text or "")
        media_msgs = [
            m for m in msgs
            if m.media and not isinstance(m.media, types.MessageMediaWebPage)
        ]
        text_msgs = [
            m for m in msgs
            if not (m.media and not isinstance(m.media, types.MessageMediaWebPage)) and m.text
        ]

        payloads: list[dict] = []
        if len(media_msgs) > 1:
            paths = await asyncio.gather(
                *(self._download_one(client, m, temp_paths) for m in media_msgs)
            )
            for m, path in zip(media_msgs, paths):
                if path:
                    payloads.append({"msg": m, "path": path, "text": None})
        else:
            for m in media_msgs:
                path = await self._download_one(client, m, temp_paths)
                if path:
                    payloads.append({"msg": m, "path": path, "text": None})
        for m in text_msgs:
            payloads.append({"msg": m, "path": None, "text": m.text})
        if not payloads:
            return None
        return {"msgs": msgs, "kind": "media", "payloads": payloads, "caption": caption}

    async def _download_one(self, client, msg, temp_paths: set[str]) -> str | None:
        try:
            ext = _media_extension(msg)
            tmp_path = os.path.join(tempfile.gettempdir(), f"fwd_{uuid.uuid4().hex}{ext}")
            temp_paths.add(tmp_path)
            path = await client.download_media(msg, file=tmp_path)
            if not path:
                temp_paths.discard(tmp_path)
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                return None
            return path
        except FloodWaitError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("download failed for msg %s: %s", msg.id, exc)
            temp_paths.discard(tmp_path)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return None

    async def _upload_item(
        self, client, cfg: TransferConfig, pkg: dict, reply_map: dict,
    ) -> None:
        # Temp files are only removed here on success. If an upload raises
        # (e.g. FloodWait) the files are kept so _with_retries can retry, and
        # any leftovers are cleaned up when the pipeline finishes.
        if pkg["kind"] == "text":
            for payload in pkg["payloads"]:
                msg = payload["msg"]
                reply_to = self._reply_to(msg, reply_map)
                sent = await client.send_message(
                    cfg.dest_entity,
                    payload["text"],
                    formatting_entities=msg.entities,
                    parse_mode=None,
                    reply_to=reply_to,
                    silent=cfg.silent,
                )
                reply_map[msg.id] = sent.id
            return

        media_payloads = [p for p in pkg["payloads"] if p["path"]]
        if len(media_payloads) > 1:
            files = [p.get("file") or p["path"] for p in media_payloads]
            sent = await client.send_file(
                cfg.dest_entity,
                files,
                caption=pkg["caption"],
                silent=cfg.silent,
            )
            if isinstance(sent, list) and len(sent) == len(media_payloads):
                for payload, s in zip(media_payloads, sent):
                    reply_map[payload["msg"].id] = s.id
            else:
                for payload in media_payloads:
                    reply_map[payload["msg"].id] = sent.id
            for payload in pkg["payloads"]:
                if payload["path"] or not payload["text"]:
                    continue
                msg = payload["msg"]
                reply_to = self._reply_to(msg, reply_map)
                s = await client.send_message(
                    cfg.dest_entity,
                    payload["text"],
                    formatting_entities=msg.entities,
                    parse_mode=None,
                    reply_to=reply_to,
                    silent=cfg.silent,
                )
                reply_map[msg.id] = s.id
        else:
            for payload in pkg["payloads"]:
                msg = payload["msg"]
                reply_to = self._reply_to(msg, reply_map)
                if payload["path"]:
                    sent = await client.send_file(
                        cfg.dest_entity,
                        payload.get("file") or payload["path"],
                        caption=pkg["caption"],
                        formatting_entities=msg.entities,
                        parse_mode=None,
                        reply_to=reply_to,
                        silent=cfg.silent,
                    )
                    reply_map[msg.id] = sent.id
                elif payload["text"]:
                    sent = await client.send_message(
                        cfg.dest_entity,
                        payload["text"],
                        formatting_entities=msg.entities,
                        parse_mode=None,
                        reply_to=reply_to,
                        silent=cfg.silent,
                    )
                    reply_map[msg.id] = sent.id
        self._remove_temp_files(pkg)

    @staticmethod
    def _remove_temp_files(pkg: dict) -> None:
        for payload in pkg["payloads"]:
            path = payload.get("path")
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    @staticmethod
    def _reply_to(msg, reply_map: dict) -> int | None:
        if msg.reply_to and msg.reply_to.reply_to_msg_id:
            return reply_map.get(msg.reply_to.reply_to_msg_id)
        return None

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
    """Internal: stop the current run because auto_resume is off / user stopped."""


engine = TransferEngine()
