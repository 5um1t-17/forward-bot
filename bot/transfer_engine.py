"""Reliable, order-preserving transfer engine.

Strategy
--------
* All modes run through one strict sequential pipeline: items are processed
  one at a time in ascending source order, so the destination always mirrors
  the source exactly. At most one download and one upload is active at any
  instant — never overlapping — which trades a little raw speed for
  long-term stability at 24/7 scale. A single large file is still fetched
  with several concurrent part-requests so big downloads stay fast.
* Forwarding uses ``forward_messages`` (native, server-side). Copying recreates
  the message server-side via ``send_file``/``send_message`` with the *existing*
  input media (no re-upload). Download & Re-upload mode (for restricted private
  groups) downloads each file to a temp file, immediately re-uploads it in
  source order, then deletes the temp file before moving to the next item.
* Message ids are processed in ascending order so reply chains can be
  mapped from source id to destination id.
* Albums (``grouped_id``) are re-grouped so they remain albums in the
  destination.
* FloodWaitError is slept through and processing resumes automatically. If a
  single operation keeps escalating its wait (cumulative > ``MAX_FLOOD_WAIT``)
  it gives up and counts the item as failed instead of hanging forever. While
  waiting, the live progress reports a per-second countdown.
* Retryable failures (FloodWait / RPC / network / timeout) retry the *whole*
  item up to ``retry_count`` times. Temp files are always removed in a
  ``finally`` so /tmp never accumulates; stale ``fwd_*`` leftovers from a
  crashed process are cleaned up at the start of every run.
* Live progress: the engine maintains a rich snapshot (current message, current
  file bytes, operation, FloodWait countdown, paused state) that is pushed to
  the ``progress_cb`` callback so the UI can refresh in place. A low-frequency
  heartbeat keeps the snapshot alive while a long transfer is in flight.
* Per-item **skip** (``request_skip``) cancels only the current message and the
  run continues with the next one. **Pause** (``request_pause`` /
  ``request_resume``) halts the run between messages without losing state.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

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

# How often the engine pushes a live snapshot while a transfer is in flight
# (the handler throttles actual message edits to PROGRESS_REFRESH).
_HEARTBEAT_INTERVAL = 0.5

# Exceptions that indicate the underlying MTProto connection is likely dead.
# When one of these is raised repeatedly, the engine asks the caller to rebuild
# the account client before the next retry.
NETWORK_ERRORS = (
    ConnectionError,
    OSError,
    asyncio.TimeoutError,
    TimeoutError,
)

ProgressCb = Callable[[dict], Awaitable[None]]
_TCoro = TypeVar("_TCoro")


@dataclass
class TransferConfig:
    source_entity: object
    dest_entity: object
    message_ids: list[int]
    mode: str = "forward"                     # forward | copy | download
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
    # Defensive: ignore empty results from `get_messages` (can be None)
    if msg is None:
        return False
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


# ----------------------------------------------------------------------
# message introspection helpers (used by the live progress UI)
# ----------------------------------------------------------------------
def _msg_type(msg) -> str:
    media = msg.media
    if media is None:
        return "Text"
    if isinstance(media, types.MessageMediaPhoto):
        return "Photo"
    if isinstance(media, types.MessageMediaDocument):
        doc = media.document
        if doc is not None:
            for attr in doc.attributes:
                if isinstance(attr, types.DocumentAttributeVideo):
                    return "Video"
                if isinstance(attr, types.DocumentAttributeAudio):
                    return "Voice" if attr.voice else "Audio"
                if isinstance(attr, types.DocumentAttributeSticker):
                    return "Sticker"
                if isinstance(attr, types.DocumentAttributeAnimated):
                    return "GIF"
            mime = (doc.mime_type or "").lower()
            if mime.startswith("video/"):
                return "Video"
            if mime.startswith("audio/"):
                return "Audio"
            if mime.startswith("image/"):
                return "Image"
        return "Document"
    if isinstance(media, types.MessageMediaPoll):
        return "Poll"
    if isinstance(media, types.MessageMediaContact):
        return "Contact"
    if isinstance(media, types.MessageMediaGeo):
        return "Location"
    if isinstance(media, types.MessageMediaWebPage):
        return "Link"
    if isinstance(media, types.MessageMediaGame):
        return "Game"
    return "Media"


def _msg_size(msg) -> int:
    media = msg.media
    if isinstance(media, types.MessageMediaDocument) and media.document:
        return media.document.size or 0
    if isinstance(media, types.MessageMediaPhoto) and media.photo:
        largest = 0
        for s in media.photo.sizes:
            try:
                largest = max(largest, s.size or 0)
            except (AttributeError, TypeError):
                continue
        return largest
    return 0


def _msg_filename(msg) -> str:
    media = msg.media
    if isinstance(media, types.MessageMediaDocument) and media.document:
        for attr in media.document.attributes:
            if isinstance(attr, types.DocumentAttributeFilename) and attr.file_name:
                return attr.file_name
        mime = media.document.mime_type or ""
        return f"file.{mime.split('/')[-1]}" if "/" in mime else "file"
    if isinstance(media, types.MessageMediaPhoto):
        return "photo.jpg"
    return ""


class TransferEngine:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._cancelled = False
        # per-item skip: only the currently processed message is abandoned
        self._skip_evt = asyncio.Event()
        self._skip_target: int | None = None
        self._current_index: int = 0
        # pause / resume: request_pause() sets the flag+event, request_resume()
        # clears them. Dense checkpoints + op cancellation make it immediate.
        self._paused = False
        self._pause_evt = asyncio.Event()
        # live progress snapshot attributes
        self._operation = "Starting"
        self._current_msg: dict | None = None
        self._file_dl: dict | None = None
        self._file_up: dict | None = None
        self._flood_wait: float | None = None
        self._mode: str = "forward"
        # account client used by the current run; refreshed on network failure
        self._client: object = None
        self._refresh_client: Callable[[], Awaitable[object]] | None = None
        # retry safety: a per-item deadline guarantees the queue always moves
        self._item_deadline: float = 0.0
        self._last_refresh_ts: float = 0.0
        # Run-level watchdog anchor: a monotonic timestamp of the *last real
        # forward progress* (job start, item start, or actual byte movement).
        # It is (re)initialised at the top of every run() so a stale value from
        # a previous job can never leak into the next one, and it is always a
        # value from the same time.monotonic() clock — never 0.0.
        self._last_activity_ts: float = 0.0

    # ------------------------------------------------------------------
    # control
    # ------------------------------------------------------------------
    def request_stop(self) -> None:
        self._cancelled = True
        self._stop.set()

    def request_skip(self) -> None:
        """Skip only the message currently being processed; keep going."""
        self._skip_evt.set()
        self._skip_target = self._current_index

    def request_pause(self) -> None:
        log.info("Pause requested")
        self._paused = True
        self._pause_evt.set()

    def request_resume(self) -> None:
        log.info("Resume requested")
        self._paused = False
        self._pause_evt.clear()

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
            try:
                async for msg in self._iter_bounded(
                    client.iter_messages(source), config.MSG_FETCH_TIMEOUT
                ):
                    if len(ids) >= count:
                        break
                    if isinstance(msg, types.MessageService):
                        continue
                    if message_matches_filter(msg, filter_type):
                        ids.append(msg.id)
                    if len(ids) >= cap:
                        break
            except asyncio.TimeoutError as exc:
                raise ValueError(
                    "Timed out scanning the source chat for messages. "
                    "Try a smaller range or check the network."
                ) from exc
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

    @staticmethod
    def _iter_bounded(aiter, timeout: float):
        """Wrap an async iterator so every ``__anext__`` has a hard timeout.

        Prevents a hung source chat from blocking message collection forever.
        """
        it = aiter.__aiter__()

        async def _anext():
            return await asyncio.wait_for(it.__anext__(), timeout=timeout)

        return _BoundedIter(_anext)

    # ------------------------------------------------------------------
    # live progress state
    # ------------------------------------------------------------------
    @staticmethod
    def _msg_link(entity, msg_id: int) -> str:
        username = getattr(entity, "username", None)
        if username:
            return f"https://t.me/{username}/{msg_id}"
        cid = getattr(entity, "id", 0)
        if cid < 0:
            cid = -cid
        s = str(cid)
        if s.startswith("100"):
            s = s[3:]
        return f"https://t.me/c/{s}/{msg_id}"

    def _set_current(self, cfg: TransferConfig, item: dict) -> None:
        msgs = item["messages"]
        first = msgs[0]
        info = {
            "link": self._msg_link(cfg.source_entity, first.id),
            "msg_id": first.id,
            "type": _msg_type(first),
            "size": _msg_size(first),
            "filename": _msg_filename(first),
        }
        if len(msgs) > 1:
            info["type"] = f"Album · {len(msgs)}"
            info["size"] = sum(_msg_size(m) for m in msgs)
        self._current_msg = info

    def _progress_state(self, result: TransferResult, cfg: TransferConfig, start: float) -> dict:
        elapsed = time.monotonic() - start
        done = result.success + result.skipped + result.failed
        speed = done / elapsed if elapsed else 0.0
        remaining = max(0, result.total - done)
        eta = remaining / speed if speed > 0 else 0.0
        return {
            "total": result.total,
            "success": result.success,
            "skipped": result.skipped,
            "failed": result.failed,
            "elapsed": elapsed,
            "speed": speed,
            "eta": eta,
            "mode": cfg.mode,
            "operation": self._operation,
            "paused": self._paused,
            "flood_wait": self._flood_wait,
            "current": self._current_msg,
            "file_dl": self._file_dl,
            "file_up": self._file_up,
            "source_name": cfg.source_name,
            "dest_name": cfg.dest_name,
        }

    def _file_progress_cb(self, filename: str, operation: str, which: str = "dl"):
        """Build a Telethon progress_callback that feeds the live snapshot.

        ``which`` selects the slot: "dl" for downloads, "up" for uploads.
        """
        slot = "_file_dl" if which == "dl" else "_file_up"
        st = {"ts": 0.0, "prev": 0.0}

        def cb(received: int, total: int) -> None:
            now = time.monotonic()
            if not st["ts"]:
                st["ts"] = now
                st["prev"] = received
            speed = 0.0
            dt = now - st["ts"]
            if dt >= 1.0:
                speed = (received - st["prev"]) / dt if dt > 0 else 0.0
                st["ts"] = now
                st["prev"] = received
            total = total or 0
            setattr(self, slot, {
                "filename": filename or "file",
                "done": received,
                "total": total,
                "speed": speed,
                "eta": (total - received) / speed if speed > 0 and total else 0.0,
            })
            self._operation = operation
            # Real byte progress means the pipeline is alive: refresh the
            # run-level watchdog anchor so a slow-but-progressing transfer is
            # never mistaken for a stall.
            self._last_activity_ts = time.monotonic()

        return cb

    # ------------------------------------------------------------------
    async def run(
        self,
        client: TelegramClient,
        cfg: TransferConfig,
        progress_cb: ProgressCb | None = None,
        refresh_client: Callable[[], Awaitable[object | None]] | None = None,
    ) -> TransferResult:
        self._stop.clear()
        self._cancelled = False
        self._skip_evt.clear()
        self._skip_target = None
        self._paused = False
        self._pause_evt.clear()
        self._operation = "Resolving Link"
        self._current_msg = None
        self._file_dl = None
        self._file_up = None
        self._flood_wait = None
        self._mode = cfg.mode
        self._client = client
        self._refresh_client = refresh_client
        self._last_refresh_ts = 0.0
        # Reset every watchdog / queue timer for this job. Using the *current*
        # monotonic clock value means the run-level watchdog can never inherit
        # a timestamp from a previous job, and can never be tripped by the host
        # having been up for a long time (see _run_watchdog).
        self._current_index = 0
        self._item_deadline = 0.0
        self._last_activity_ts = time.monotonic()
        result = TransferResult(total=len(cfg.message_ids))
        start = time.monotonic()
        state_builder = lambda: self._progress_state(result, cfg, start)  # noqa: E731
        log.info(
            "Worker started: mode=%s total=%d forward_delay=%s "
            "architecture=strict download_workers=1 upload_workers=1 intra_file_parts=%s",
            cfg.mode,
            len(cfg.message_ids),
            cfg.forward_delay,
            config.DOWNLOAD_PARTS,
        )
        self.cleanup_stale_temp()
        if progress_cb is not None:
            await progress_cb(state_builder())

        beat_task: asyncio.Task | None = None
        if progress_cb is not None:
            beat_task = asyncio.create_task(self._heartbeat(progress_cb, state_builder))

        watchdog_task: asyncio.Task | None = None
        watchdog_task = asyncio.create_task(self._run_watchdog())

        reply_map: dict[int, int] = {}
        ids = sorted(cfg.message_ids)
        aborted = False
        try:
            items, fetch_failed = await self._collect_items(cfg, ids)
            if fetch_failed:
                result.failed += fetch_failed
            i = 0
            while i < len(items):
                item = items[i]
                self._current_index = i
                if self._stop.is_set():
                    break
                if not await self._pause_gate(progress_cb, state_builder):
                    break
                if self._skip_evt.is_set():
                    self._skip_evt.clear()
                    self._skip_target = None
                    result.skipped += item["count"]
                    self._set_current(cfg, item)
                    self._operation = "Skipping"
                    self._flood_wait = None
                    if progress_cb is not None:
                        await progress_cb(state_builder())
                    i += 1
                    continue
                self._set_current(cfg, item)
                self._operation = _verb(cfg.mode)
                if progress_cb is not None:
                    await progress_cb(state_builder())
                self._item_deadline = time.monotonic() + config.MAX_ITEM_RETRY_SECONDS
                # Reset the run watchdog anchor when a new item begins so a
                # previous item's timer can never carry over to this one.
                self._last_activity_ts = time.monotonic()
                try:
                    await self._process_item(
                        cfg, item, result, reply_map, skip_idx=i,
                        progress_cb=progress_cb, state_builder=state_builder,
                    )
                except _Paused:
                    log.info("Worker paused mid-item (index %d)", i)
                    if not await self._pause_gate(progress_cb, state_builder):
                        break
                    continue  # retry the same item after resume
                except _SkipCurrent:
                    self._skip_evt.clear()
                    self._skip_target = None
                    result.skipped += item["count"]
                    i += 1
                    continue
                except _Stopped:
                    log.info("Worker stopped mid-item (index %d)", i)
                    aborted = True
                    break
                except _Abort:
                    aborted = True
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("item failed: %s", exc)
                    result.failed += item["count"]
                    i += 1
                    continue
                i += 1
                self._current_index = i
                if cfg.forward_delay and not self._stop.is_set():
                    try:
                        await self._sleep_interruptible(cfg.forward_delay, skip_idx=i)
                    except _SkipCurrent:
                        pass
                    except _Paused:
                        if not await self._pause_gate(progress_cb, state_builder):
                            break
                if progress_cb is not None:
                    await progress_cb(state_builder())
        except asyncio.CancelledError:
            result.cancelled = True
            self._stop.set()
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("transfer run aborted")
            result.cancelled = True
            result.error = str(exc)
        finally:
            self._stop.set()
            for task in (watchdog_task, beat_task):
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

        result.duration = time.monotonic() - start
        log.info(
            "Worker stopped: mode=%s total=%d success=%d skipped=%d failed=%d cancelled=%s duration=%.1fs",
            cfg.mode,
            result.total,
            result.success,
            result.skipped,
            result.failed,
            result.cancelled,
            result.duration,
        )
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

    async def _collect_items(self, cfg, ids: list[int]) -> tuple[list[dict], int]:
        items: list[dict] = []
        fetch_failed = 0
        for chunk_start in range(0, len(ids), config.BATCH_SIZE):
            if self._stop.is_set():
                break
            chunk = ids[chunk_start:chunk_start + config.BATCH_SIZE]
            log.debug("pipeline: collect chunk %d..%d (%d msgs)", chunk_start, chunk_start + len(chunk), len(chunk))
            msgs, failed = await self._fetch_existing(cfg.source_entity, chunk)
            log.debug("pipeline: collect chunk returned %d msgs, %d failed", len(msgs), failed)
            if failed:
                log.warning("skipping %d message(s): chunk fetch failed", failed)
                fetch_failed += failed
            items.extend(self._build_items(msgs, cfg))
        log.debug("pipeline: collected %d items from %d ids (%d fetch failures)", len(items), len(ids), fetch_failed)
        return items, fetch_failed

    # ------------------------------------------------------------------
    async def _fetch_existing(self, source, chunk: list[int]) -> tuple[list, int]:
        """Fetch one chunk with retries and a hard timeout.

        Returns ``(found_messages, failed_count)``. Never hangs: after retries
        the chunk is abandoned and counted as failed so the run moves on.
        """
        attempts = 0
        while True:
            if self._stop.is_set():
                return [], len(chunk)
            log.debug("pipeline: get_messages await chunk=%d msgs (attempt %d)", len(chunk), attempts)
            try:
                found = await asyncio.wait_for(
                    self._client.get_messages(source, ids=chunk),
                    timeout=config.MSG_FETCH_TIMEOUT,
                )
                log.debug("pipeline: get_messages returned %d msgs", len(found) if isinstance(found, list) else 1)
            except (MessageIdInvalidError, ValueError):
                found = []
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                attempts += 1
                log.warning("message fetch failed (attempt %d): %s", attempts, exc)
                if self._stop.is_set() or attempts >= 3:
                    return [], len(chunk)
                try:
                    await self._sleep_interruptible(min(2 * attempts, 10), skip_idx=None)
                except (_SkipCurrent, _Paused):
                    pass
                continue
            if not isinstance(found, list):
                found = [found] if found else []
            return found, 0

    def _build_items(self, msgs: list, cfg: TransferConfig) -> list[dict]:
        groups: dict[int, list] = {}
        singles: list[dict] = []
        for msg in msgs:
            # skip None entries which can appear when messages are missing
            if msg is None:
                continue
            if not message_matches_filter(msg, cfg.filter_type):
                continue
            # grouped_id may be falsy / None for non-album messages
            if getattr(msg, "grouped_id", None):
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
    @staticmethod
    async def _heartbeat(progress_cb: ProgressCb, state_builder) -> None:
        """Push a live snapshot periodically so a long transfer never looks
        frozen. Cancelled by :meth:`run` when the run finishes."""
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            try:
                await progress_cb(state_builder())
            except Exception:  # noqa: BLE001
                log.debug("heartbeat progress update failed", exc_info=True)

    async def _run_watchdog(self) -> None:
        """Abort the run if the engine makes no forward progress for too long.

        The check is ``time.monotonic() - self._last_activity_ts`` where
        ``_last_activity_ts`` is (re)set at job start, at every item start and
        whenever real download/upload bytes advance. That makes the watchdog:

        * immune to stale timestamps from a previous job (the anchor is always
          re-initialised from the same monotonic clock at the top of run()),
        * immune to the host's monotonic clock having run for a long time (a
          deadline of ``0.0`` is never subtracted — the old code did exactly
          that and tripped the moment the host had been up > MAX_ITEM_RETRY_SECONDS,
          which is why a fresh worker reported "stuck on item 0 for >900s" 30s
          after starting),
        * correct in both directions: it fires only when *no* forward progress
          has happened for longer than ``MAX_ITEM_RETRY_SECONDS``, and it never
          fires while the run is paused.

        Hung *operations* (a single download/upload that stops reporting bytes)
        are handled by ``_guard``'s per-op stall watchdog + hard timeouts; this
        run-level watchdog is the last-resort backstop for a completely wedged
        loop.
        """
        stall_limit = config.MAX_ITEM_RETRY_SECONDS
        check_interval = config.WATCHDOG_INTERVAL
        while True:
            await asyncio.sleep(check_interval)
            if self._stop.is_set():
                break
            # A deliberate user pause is not a stall — never kill a paused run.
            if self._paused:
                continue
            elapsed = time.monotonic() - self._last_activity_ts
            if elapsed > stall_limit:
                log.warning(
                    "Watchdog: no forward progress for >%.0fs (item %d, last activity %.0fs ago); forcing stop",
                    stall_limit, self._current_index, elapsed,
                )
                self.request_stop()
                break

    @staticmethod
    def cleanup_stale_temp(max_age: float = 3600.0) -> int:
        """Remove leftover ``fwd_*`` temp files older than ``max_age`` seconds.

        Called at the start of every run (and at process startup), so a
        previously crashed process can never leave files accumulating in /tmp.
        """
        removed = 0
        try:
            for name in os.listdir(tempfile.gettempdir()):
                if not name.startswith("fwd_"):
                    continue
                path = os.path.join(tempfile.gettempdir(), name)
                try:
                    if time.time() - os.path.getmtime(path) >= max_age:
                        os.remove(path)
                        removed += 1
                except OSError:
                    continue
        except OSError:
            return 0
        if removed:
            log.info("Cleaned up %d stale temp file(s)", removed)
        return removed

    # ------------------------------------------------------------------
    async def _pause_gate(self, progress_cb, state_builder) -> bool:
        """Wait at a checkpoint while paused; returns False if stopped meanwhile.

        Also called after a mid-item pause so the queue resumes exactly where
        it was interrupted.
        """
        if not self._pause_evt.is_set():
            return True
        log.info("Worker paused")
        while self._pause_evt.is_set():
            if self._stop.is_set():
                return False
            self._operation = "Paused"
            if progress_cb is not None and state_builder is not None:
                state = state_builder()
                state["operation"] = "Paused"
                state["paused"] = True
                try:
                    await progress_cb(state)
                except Exception:
                    pass
            await asyncio.sleep(1.0)
        self._operation = _verb(self._mode) if self._mode else "Transferring"
        log.info("Worker resumed")
        return True

    # ------------------------------------------------------------------
    async def _guard(
        self,
        coro,
        *,
        timeout: float | None = None,
        progress: Callable[[], int | None] | None = None,
        stall_timeout: float | None = None,
        opname: str = "operation",
    ):
        """Run a network operation with a hard timeout and a stall watchdog.

        * ``timeout`` — hard wall-clock limit for quick operations.
        * ``progress`` + ``stall_timeout`` — cancels when no progress is
          reported for ``stall_timeout`` seconds (long downloads/uploads that
          have no sane wall-clock limit).
        * Pause: while the op is in flight, ``request_pause`` cancels it and
          raises :class:`_Paused` so the item is retried after resume.

        Nothing can hang forever: every path either returns the result, raises
        the underlying error, raises ``TimeoutError`` (retried upstream) or
        raises ``_Paused``.
        """
        if self._pause_evt.is_set():
            raise _Paused()
        task = asyncio.create_task(coro)
        op_start = time.monotonic()
        last_ts = time.monotonic()
        last_prog = progress() if progress is not None else None
        seen_progress = progress is not None and last_prog is not None
        deadline = (time.monotonic() + timeout) if timeout else None
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=config.CONTROL_POLL)
                if task in done:
                    result = task.result()
                    log.debug("pipeline: op %s completed after %.2fs", opname, time.monotonic() - op_start)
                    return result
                now = time.monotonic()
                if self._stop.is_set():
                    log.info("Stop detected in %s, cancelling", opname)
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.wait_for(task, timeout=5.0)
                    raise _Stopped()
                if self._pause_evt.is_set():
                    log.info("Pause interrupted %s, cancelling", opname)
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.wait_for(task, timeout=5.0)
                    raise _Paused()
                if progress is not None:
                    cur = progress()
                    # Only apply the stall watchdog once a real progress value
                    # has been observed: an op whose callback never fires (e.g.
                    # some album uploads) must never be cancelled for "no
                    # progress" while it is actually making progress.
                    if cur is not None and not seen_progress:
                        seen_progress = True
                        last_prog = cur
                        last_ts = now
                    elif seen_progress:
                        if cur != last_prog:
                            last_prog = cur
                            last_ts = now
                        elif stall_timeout and now - last_ts >= stall_timeout:
                            log.warning(
                                "Watchdog: %s stalled (%ss with no progress), cancelling",
                                opname, stall_timeout,
                            )
                            task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await asyncio.wait_for(task, timeout=5.0)
                            raise asyncio.TimeoutError(
                                f"{opname} stalled ({stall_timeout:.0f}s no progress)"
                            )
                if deadline is not None and now >= deadline:
                    log.warning("Watchdog: %s timed out after %ss", opname, timeout)
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.wait_for(task, timeout=5.0)
                    raise asyncio.TimeoutError(f"{opname} timed out after {timeout:.0f}s")
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5.0)

    def _dl_progress(self) -> int | None:
        return (self._file_dl or {}).get("done")

    def _up_progress(self) -> int | None:
        return (self._file_up or {}).get("done")

    async def _download_one(self, msg, temp_paths: set[str]) -> str | None:
        """Download one message's media to a temp file and verify it.

        Returns the temp path on success, or ``None`` when the media is simply
        unavailable. Any transport error (network / RPC / FloodWait) is
        re-raised so :meth:`_with_retries` can retry the whole item; the temp
        file is left registered in ``temp_paths`` and removed by the caller's
        ``finally`` so nothing can linger in /tmp.
        """
        ext = _media_extension(msg)
        tmp_path = os.path.join(tempfile.gettempdir(), f"fwd_{uuid.uuid4().hex}{ext}")
        temp_paths.add(tmp_path)
        # Seed the download snapshot so the _guard stall watchdog is armed from
        # the very first poll (done=0, not None). Without this a download whose
        # first progress report never arrives (e.g. the parallel part-path
        # wedging on its first request) would report no progress, keep
        # seen_progress=False and hang forever.
        self._file_dl = {
            "filename": _msg_filename(msg) or f"message_{msg.id}",
            "done": 0,
            "total": 0,
            "speed": 0.0,
            "eta": 0.0,
        }
        progress = self._file_progress_cb(
            _msg_filename(msg) or f"message_{msg.id}", "Downloading", "dl"
        )
        log.info("Download started: msg=%d -> %s", msg.id, tmp_path)
        try:
            path = await self._download_media(msg, tmp_path, progress)
        except asyncio.CancelledError:
            raise
        except _Paused:
            log.info("Download paused: msg=%d", msg.id)
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("download failed for msg %s: %s", msg.id, exc)
            raise
        if not path:
            log.warning("no media returned for msg %s", msg.id)
            return None
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size <= 0:
            log.warning("download produced an empty file for msg %s", msg.id)
            return None
        log.info("Download finished: msg=%d (%.1f MB)", msg.id, size / (1024 * 1024))
        return path

    async def _download_media(self, msg, tmp_path: str, progress) -> str | None:
        """Download one message's media, optionally parallelising large documents."""
        media = msg.media
        doc = media.document if isinstance(media, types.MessageMediaDocument) else None
        size = int(getattr(doc, "size", 0) or 0)
        log.debug("pipeline: _download_media enter msg=%d size=%d path=%s", msg.id, size, tmp_path)
        # Strict queue architecture: ONE download at a time and (by default) one
        # part at a time. Intra-file part-parallelism is an explicit opt-in via
        # DOWNLOAD_PARTS > 1; it never changes the number of simultaneous files.
        if (
            doc is not None
            and size >= config.DOWNLOAD_PARALLEL_MIN
            and config.DOWNLOAD_PARTS > 1
        ):
            location = types.InputDocumentFileLocation(
                id=doc.id,
                access_hash=doc.access_hash,
                file_reference=doc.file_reference,
                thumb_size="",
            )
            try:
                log.debug("pipeline: parallel download start msg=%d parts=%d", msg.id, config.DOWNLOAD_PARTS)
                result = await self._download_media_parallel(
                    location, size, tmp_path, progress, config.DOWNLOAD_PARTS
                )
                log.debug("pipeline: parallel download done msg=%d", msg.id)
                return result
            except _Paused:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("parallel download failed (msg %s), falling back: %s", msg.id, exc)
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        log.debug("pipeline: sequential download await msg=%d", msg.id)
        try:
            result = await self._guard(
                self._client.download_media(msg, file=tmp_path, progress_callback=progress),
                progress=self._dl_progress,
                stall_timeout=config.STALL_TIMEOUT,
                opname=f"download msg={msg.id}",
            )
        except Exception:
            log.debug("pipeline: sequential download raised msg=%d", msg.id, exc_info=True)
            raise
        log.debug("pipeline: sequential download returned msg=%d", msg.id)
        return result

    async def _download_media_parallel(
        self, location, file_size: int, dest_path: str,
        progress, parts: int,
    ) -> str:
        """Download one file using ``parts`` concurrent part requests.

        Wrapped in :meth:`_guard` so the whole parallel transfer is
        cancellable on pause/stop and bounded by the stall watchdog.
        """
        part_size = 512 * 1024
        total_parts = max(1, (file_size + part_size - 1) // part_size)
        n = max(1, min(parts, total_parts, 8))
        with open(dest_path, "wb") as f:
            f.truncate(file_size)

        state = {"done": 0}

        def report(delta: int) -> None:
            state["done"] += delta
            if progress is not None:
                progress(state["done"], file_size)

        async def worker(i: int) -> None:
            start_part = (i * total_parts) // n
            end_part = ((i + 1) * total_parts) // n
            if end_part <= start_part:
                return
            offset = start_part * part_size
            num = end_part - start_part
            iterator = self._client.iter_download(
                location,
                file_size=file_size,
                chunk_size=part_size,
                request_size=part_size,
                offset=offset,
                stride=part_size,
                limit=num,
            )
            pos = offset
            log.debug(
                "pipeline: parallel worker %d start offset=%d parts=%d", i, offset, num
            )
            with open(dest_path, "r+b") as f:
                async for chunk in iterator:
                    f.seek(pos)
                    f.write(chunk)
                    report(len(chunk))
                    pos += len(chunk)
                    log.debug(
                        "pipeline: parallel worker %d chunk +%d (pos=%d)", i, len(chunk), pos
                    )
            log.debug("pipeline: parallel worker %d done (offset=%d)", i, offset)

        async def run_workers() -> str:
            log.debug("pipeline: parallel download gather start (%d workers)", n)
            await asyncio.gather(*(asyncio.create_task(worker(i)) for i in range(n)))
            log.debug("pipeline: parallel download gather complete")
            return dest_path

        log.debug("pipeline: parallel download guard start (file_size=%d)", file_size)
        try:
            result = await self._guard(
                run_workers(),
                progress=self._dl_progress,
                stall_timeout=config.STALL_TIMEOUT,
                opname=f"parallel download ({file_size / (1024 * 1024):.0f} MB)",
            )
        except Exception:
            log.debug("pipeline: parallel download guard raised", exc_info=True)
            raise
        log.debug("pipeline: parallel download guard returned")
        return result


    @staticmethod
    def _reply_to(msg, reply_map: dict) -> int | None:
        if msg.reply_to and msg.reply_to.reply_to_msg_id:
            return reply_map.get(msg.reply_to.reply_to_msg_id)
        return None

    # ------------------------------------------------------------------
    async def _process_item(
        self, cfg: TransferConfig, item: dict, result: TransferResult,
        reply_map: dict, skip_idx: int | None = None,
        progress_cb: ProgressCb | None = None, state_builder=None,
    ) -> None:
        msgs = item["messages"]
        src_id, dst_id = cfg.source_entity.id, cfg.dest_entity.id

        if cfg.dedup:
            for m in msgs:
                if await db.is_transferred(src_id, dst_id, m.id):
                    result.skipped += len(msgs)
                    return

        await self._with_retries(
            cfg, lambda: self._transfer(cfg, msgs, reply_map, progress_cb, state_builder),
            skip_idx=skip_idx, progress_cb=progress_cb, state_builder=state_builder,
        )

        sent_count = 0
        for m in msgs:
            if m.id in reply_map:
                await db.mark_transferred(src_id, dst_id, m.id, cfg.sid, cfg.mode)
                sent_count += 1
        result.success += sent_count

    async def _with_retries(
        self, cfg, fn, skip_idx: int | None = None,
        progress_cb: ProgressCb | None = None, state_builder=None,
    ) -> None:
        attempts = 0
        flood_total = 0.0
        while True:
            if self._item_deadline and time.monotonic() >= self._item_deadline:
                log.warning(
                    "Item deadline reached (%.0fs elapsed), skipping item",
                    config.MAX_ITEM_RETRY_SECONDS,
                )
                raise _SkipCurrent()
            try:
                return await fn()
            except _Abort:
                raise
            except _SkipCurrent:
                raise
            except _Paused:
                raise
            except _Stopped:
                raise
            except FloodWaitError as exc:
                if not cfg.handle_flood:
                    raise
                wait = exc.seconds
                flood_total += wait
                if flood_total > config.MAX_FLOOD_WAIT:
                    log.warning(
                        "flood wait keeps escalating (%.0fs total), giving up on this item",
                        flood_total,
                    )
                    raise
                sleep_for = min(wait, config.MAX_FLOOD_SLEEP) + config.FLOOD_BUFFER
                log.info("FloodWait %.1fs, sleeping %.1fs", wait, sleep_for)
                self._flood_wait = sleep_for
                self._operation = "Waiting FloodWait"
                try:
                    deadline = time.monotonic() + sleep_for
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._flood_wait = remaining
                        # Sleeping through a flood wait is legitimate forward
                        # movement: keep the run watchdog satisfied.
                        self._last_activity_ts = time.monotonic()
                        if progress_cb is not None and state_builder is not None:
                            await progress_cb(state_builder())
                        ok = await self._sleep_interruptible(
                            min(1.0, remaining), skip_idx=skip_idx
                        )
                        if not ok:
                            raise _Abort()
                finally:
                    self._flood_wait = None
                continue
            except ChatForwardsRestrictedError:
                if cfg.mode == "forward":
                    log.info("Forwarding restricted, falling back to download mode")
                    cfg.mode = "download"
                    continue
                attempts += 1
                if cfg.retry_count == 0 or attempts <= cfg.retry_count:
                    await self._sleep_interruptible(min(2 * attempts, 10), skip_idx=skip_idx)
                    continue
                if not cfg.auto_resume:
                    raise _Abort()
                raise
            except (MessageIdInvalidError, MessageEmptyError):
                attempts += 1
                if cfg.retry_count == 0 or attempts <= cfg.retry_count:
                    await self._sleep_interruptible(min(2 * attempts, 10), skip_idx=skip_idx)
                    continue
                if not cfg.auto_resume:
                    raise _Abort()
                raise
            except Exception as exc:  # noqa: BLE001 - retry generic failures too
                log.warning("transfer error: %s", exc)
                attempts += 1
                if isinstance(exc, NETWORK_ERRORS):
                    await self._refresh_if_needed()
                if cfg.retry_count == 0 or attempts <= cfg.retry_count:
                    await self._sleep_interruptible(min(2 * attempts, 10), skip_idx=skip_idx)
                    continue
                if not cfg.auto_resume:
                    raise _Abort()
                raise

    async def _refresh_if_needed(self) -> None:
        """Rebuild the account client on repeated network errors.

        ``refresh_client`` (wired from the handler) re-creates the Telethon
        client for this account so a dead MTProto connection self-heals
        instead of retrying against a half-dead client forever. Guarded by a
        cooldown so we never hammer the session factory.
        """
        if self._refresh_client is None:
            return
        now = time.monotonic()
        if now - self._last_refresh_ts < config.RECONNECT_DELAY:
            return
        self._last_refresh_ts = now
        log.info("Network failure detected, rebuilding account client...")
        try:
            new_client = await self._refresh_client()
        except Exception as exc:  # noqa: BLE001
            log.warning("client refresh failed: %s", exc)
            return
        if new_client is None:
            log.warning("client refresh returned no client; keeping current one")
            return
        self._client = new_client
        log.info("Account client rebuilt after network failure")

    async def _sleep_interruptible(self, seconds: float, skip_idx: int | None = None) -> bool:
        """Sleep in slices; return False if stop was requested.

        Raises ``_SkipCurrent`` when a skip was requested for ``skip_idx`` and
        raises ``_Paused`` when a pause interrupts the sleep so the current
        item is retried after resume.
        """
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stop.is_set():
                raise _Stopped()
            if self._skip_target is not None and self._skip_target == skip_idx:
                raise _SkipCurrent()
            if self._pause_evt.is_set():
                raise _Paused()
            await asyncio.sleep(min(1.0, end - time.monotonic()))
        return True

    # ------------------------------------------------------------------
    async def _transfer(self, cfg, msgs: list, reply_map: dict, progress_cb=None, state_builder=None) -> None:
        if cfg.mode == "forward":
            await self._forward(cfg, msgs, reply_map)
        elif cfg.mode == "copy":
            await self._copy(cfg, msgs, reply_map)
        elif cfg.mode == "download":
            await self._download_transfer(cfg, msgs, reply_map, progress_cb, state_builder)

    async def _forward(self, cfg, msgs: list, reply_map: dict) -> None:
        ids = [m.id for m in msgs]
        as_album = len(msgs) > 1
        drop_captions = "remove_captions" in cfg.options
        drop_author = "hide_header" in cfg.options
        sent = await self._guard(
            self._client.forward_messages(
                cfg.dest_entity,
                ids,
                from_peer=cfg.source_entity,
                as_album=as_album,
                drop_media_captions=drop_captions,
                drop_author=drop_author,
                silent=cfg.silent,
            ),
            timeout=config.OP_TIMEOUT,
            opname=f"forward msg(s) {[m.id for m in msgs]}",
        )
        if as_album:
            dest_ids = [s.id for s in sent]
        else:
            dest_ids = [sent.id]
        for m, did in zip(msgs, dest_ids):
            reply_map[m.id] = did

    async def _copy(self, cfg, msgs: list, reply_map: dict) -> None:
        if len(msgs) > 1:
            await self._copy_album(cfg, msgs, reply_map)
            return
        m = msgs[0]
        did = await self._copy_one(cfg, m, reply_map)
        if did is not None:
            reply_map[m.id] = did

    async def _copy_album(self, cfg, msgs: list, reply_map: dict) -> None:
        text_only = "text_only" in cfg.options
        if text_only:
            for m in msgs:
                did = await self._copy_one(cfg, m, reply_map)
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
                did = await self._copy_one(cfg, m, reply_map)
                if did is not None:
                    reply_map[m.id] = did
            return
        sent = await self._guard(
            self._client.send_file(
                cfg.dest_entity, media, caption=caption, silent=cfg.silent
            ),
            timeout=config.OP_TIMEOUT,
            progress=self._up_progress,
            stall_timeout=config.STALL_TIMEOUT,
            opname=f"copy album ({len(media)} media)",
        )
        if isinstance(sent, list):
            dest_ids = [s.id for s in sent]
            if len(dest_ids) == len(msgs):
                for m, did in zip(msgs, dest_ids):
                    reply_map[m.id] = did
                return
        # fallback: copy individually
        for m in msgs:
            did = await self._copy_one(cfg, m, reply_map)
            if did is not None:
                reply_map[m.id] = did

    async def _copy_one(self, cfg, msg, reply_map: dict) -> int | None:
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
            sent = await self._guard(
                self._client.send_message(
                    cfg.dest_entity,
                    text,
                    formatting_entities=msg.entities,
                    parse_mode=None,
                    reply_to=reply_to,
                    silent=cfg.silent,
                ),
                timeout=config.OP_TIMEOUT,
                opname=f"send text msg={msg.id}",
            )
            return sent.id

        if media_only:
            text = ""
        else:
            text = "" if drop_caption else (msg.text or "")

        media = msg.media
        is_webpage = isinstance(media, types.MessageMediaWebPage)
        if media and not is_webpage:
            sent = await self._guard(
                self._client.send_file(
                    cfg.dest_entity,
                    media,
                    caption=text,
                    formatting_entities=msg.entities,
                    parse_mode=None,
                    reply_to=reply_to,
                    silent=cfg.silent,
                ),
                timeout=config.OP_TIMEOUT,
                progress=self._up_progress,
                stall_timeout=config.STALL_TIMEOUT,
                opname=f"send file msg={msg.id}",
            )
            return sent.id

        if text:
            sent = await self._guard(
                self._client.send_message(
                    cfg.dest_entity,
                    text,
                    formatting_entities=msg.entities,
                    parse_mode=None,
                    reply_to=reply_to,
                    silent=cfg.silent,
                ),
                timeout=config.OP_TIMEOUT,
                opname=f"send text msg={msg.id}",
            )
            return sent.id
        return None

    async def _download_transfer(self, cfg, msgs: list, reply_map: dict, progress_cb=None, state_builder=None) -> None:
        """Strictly sequential download & re-upload of a single item.

        Lifecycle per file: download -> verify -> upload -> delete, one file
        at a time, so at most one download and one upload is ever active.
        Albums are downloaded file-by-file then re-grouped into a single
        ``send_file`` album. The ``finally`` guarantees every temp file is
        removed on *every* path (success, retry, skip, stop), so /tmp can
        never accumulate. Exceptions are intentionally not swallowed here:
        :meth:`_with_retries` retries the whole item, then the run counts it
        as failed instead of silently dropping a file.
        """
        text_only = "text_only" in cfg.options
        media_only = "media_only" in cfg.options
        drop_caption = "remove_captions" in cfg.options

        if text_only:
            for m in msgs:
                text = m.text
                if not text:
                    continue
                sent = await self._guard(
                    self._client.send_message(
                        cfg.dest_entity,
                        text,
                        formatting_entities=m.entities,
                        parse_mode=None,
                        reply_to=self._reply_to(m, reply_map),
                        silent=cfg.silent,
                    ),
                    timeout=config.OP_TIMEOUT,
                    opname=f"send text msg={m.id}",
                )
                reply_map[m.id] = sent.id
            return

        is_album = len(msgs) > 1
        caption = "" if (media_only or drop_caption) else (msgs[0].text or "")

        media_msgs: list = []
        text_msgs: list = []
        for m in msgs:
            media = m.media
            is_webpage = isinstance(media, types.MessageMediaWebPage)
            if media and not is_webpage:
                media_msgs.append(m)
            elif m.text:
                text_msgs.append(m)

        temp_paths: set[str] = set()
        try:
            if media_msgs:
                if is_album:
                    pairs: list = []
                    for m in media_msgs:
                        log.debug("pipeline: album download await msg=%d", m.id)
                        path = await self._download_one(m, temp_paths)
                        log.debug("pipeline: album download returned msg=%d path=%s", m.id, path)
                        if path:
                            pairs.append((m, path))
                    if pairs:
                        if not await self._pause_gate(progress_cb, state_builder):
                            raise _Abort()
                        log.info(
                            "uploading album of %d file(s) to %s",
                            len(pairs), cfg.dest_name or cfg.dest_entity.id,
                        )
                        up_cb = self._file_progress_cb(
                            _msg_filename(pairs[0][0]) or "album", "Uploading", "up"
                        )
                        self._file_up = {
                            "filename": _msg_filename(pairs[0][0]) or "album",
                            "done": 0,
                            "total": 0,
                            "speed": 0.0,
                            "eta": 0.0,
                        }
                        log.debug("pipeline: album upload await (%d files)", len(pairs))
                        sent = await self._guard(
                            self._client.send_file(
                                cfg.dest_entity,
                                [p for _, p in pairs],
                                caption=caption,
                                silent=cfg.silent,
                                progress_callback=up_cb,
                            ),
                            progress=self._up_progress,
                            stall_timeout=config.STALL_TIMEOUT,
                            opname=f"upload album ({len(pairs)} files)",
                        )
                        log.debug("pipeline: album upload returned (%d files)", len(pairs))
                        if isinstance(sent, list) and len(sent) == len(pairs):
                            for (m, _), s in zip(pairs, sent):
                                reply_map[m.id] = s.id
                        else:
                            for m, _ in pairs:
                                reply_map[m.id] = sent.id
                else:
                    for m in media_msgs:
                        if not await self._pause_gate(progress_cb, state_builder):
                            raise _Abort()
                        self._file_dl = None
                        self._file_up = None
                        log.debug("pipeline: download await msg=%d", m.id)
                        path = await self._download_one(m, temp_paths)
                        log.debug("pipeline: download returned msg=%d path=%s", m.id, path)
                        if not path:
                            continue
                        up_cb = self._file_progress_cb(
                            _msg_filename(m) or f"message_{m.id}", "Uploading", "up"
                        )
                        # Seed the upload snapshot so the _guard stall watchdog is
                        # armed from the first poll (done=0), never None.
                        self._file_up = {
                            "filename": _msg_filename(m) or f"message_{m.id}",
                            "done": 0,
                            "total": 0,
                            "speed": 0.0,
                            "eta": 0.0,
                        }
                        log.debug("pipeline: upload await msg=%d", m.id)
                        sent = await self._guard(
                            self._client.send_file(
                                cfg.dest_entity,
                                path,
                                caption=caption,
                                formatting_entities=m.entities,
                                parse_mode=None,
                                reply_to=self._reply_to(m, reply_map),
                                silent=cfg.silent,
                                progress_callback=up_cb,
                            ),
                            progress=self._up_progress,
                            stall_timeout=config.STALL_TIMEOUT,
                            opname=f"upload msg={m.id}",
                        )
                        log.debug("pipeline: upload returned msg=%d", m.id)
                        reply_map[m.id] = sent.id
                        log.info(
                            "item msg=%d downloaded+uploaded (%.1f MB)",
                            m.id, os.path.getsize(path) / (1024 * 1024),
                        )
            for m in text_msgs:
                sent = await self._guard(
                    self._client.send_message(
                        cfg.dest_entity,
                        m.text,
                        formatting_entities=m.entities,
                        parse_mode=None,
                        reply_to=self._reply_to(m, reply_map),
                        silent=cfg.silent,
                    ),
                    timeout=config.OP_TIMEOUT,
                    opname=f"send text msg={m.id}",
                )
                reply_map[m.id] = sent.id
        finally:
            for path in temp_paths:
                try:
                    os.remove(path)
                except OSError:
                    pass
            temp_paths.clear()


class _Paused(Exception):
    """Internal: pause interrupted the current op; item is retried on resume."""



def _verb(mode: str) -> str:
    if mode == "forward":
        return "Forwarding"
    if mode == "download":
        return "Downloading"
    return "Copying"


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


class _Stopped(Exception):
    """Internal: stop was requested mid-operation."""


class _SkipCurrent(Exception):
    """Internal: skip only the currently processed message, then continue."""


class _BoundedIter:
    """Async wrapper that imposes a per-step timeout on an async iterator.

    ``collect_ids`` uses this so a hung source chat can never block message
    scanning forever: every ``__anext__`` is awaited under a hard deadline.
    """

    def __init__(self, anext: Callable[[], Awaitable]):
        self._anext = anext

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self._anext()
        except asyncio.TimeoutError:
            raise StopAsyncIteration from None


engine = TransferEngine()
