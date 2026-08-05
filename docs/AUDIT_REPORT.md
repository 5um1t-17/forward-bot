# Stability Audit & Engine Refactor Report

Audit scope: `bot/transfer_engine.py`, `bot/client_pool.py`, `bot/session_manager.py`,
`bot/scheduler.py`, `bot/main.py`, and the test suite. All 17 tests pass
(`PYTHONPATH=. python3 -m pytest tests/ -q -p asyncio --asyncio-mode=auto`).

## 1. Findings & fixes by file

### `bot/transfer_engine.py` (engine)
| # | Finding | Fix |
|---|---------|-----|
| 1 | **Two divergent run paths.** `run()` dispatched `mode == "download"` to `_run_download_pipeline` and every other mode to a serial loop. The pipeline ran up to `threads * DOWNLOAD_MULT` downloads **and** up to `threads * UPLOAD_MULT` pre-uploads concurrently — i.e. many simultaneous downloads/uploads overlapping, exactly the "overlap accumulation" the 24/7 spec forbids. | Removed `_run_download_pipeline`, `_download_item`, `_upload_item`, `_remove_temp_files` (about 240 lines). All modes now run through **one strict sequential loop**: one item at a time, one download → verify → upload → delete → next. |
| 2 | **`_download_transfer` swallowed every exception** (lines 1382/1390 `except Exception: log.warning(...)`), so FloodWait/timeout/network failures were logged but **never retried** by `_with_retries` and files were silently dropped. | Rewrote it as a strict serial worker that does **not** swallow exceptions — `_with_retries` (already wrapping the item) now retries the whole item; after retries are exhausted the run counts it as failed. Only genuinely-unavailable media (`download_media` returns `None`) is skipped. |
| 3 | **Album temp files leaked on `send_file` failure.** In the old album branch, files were removed only *after* a successful `send_file`; a failure mid-album left partial files in `/tmp`. | Every downloaded path is registered in a `temp_paths` set; a `finally` removes **all** of them on every exit path (success, retry, skip, stop, exception). |
| 4 | **Partial-file leak on download error.** `_download_one` kept a `fwd_*` file on disk if `client.download_media` raised mid-write (the `path` var stayed `None`, so the `finally` skipped it). | The temp path is now created and registered **before** the download; the caller's `finally` removes it regardless of outcome. |
| 5 | **No verification step.** A download could return an empty/partial path and still be "uploaded". | Added a verify step: `os.path.getsize(path) > 0` check after download; empty files are discarded (logged). |
| 6 | **Slow single file = frozen UI.** The old serial loop only pushed progress *between* items, so a large file's download looked frozen (and `test_download_progress_ticks` relied on the pipeline's 0.5s timeout ticks). | Added `_heartbeat` (0.5s) that pushes a live snapshot during long transfers; cancelled in `run()`'s `finally` (also covers `_collect_items` now that it runs inside the try). |
| 7 | **`/tmp` could accumulate across crashes.** No startup cleanup existed. | Added `cleanup_stale_temp(max_age=3600)` — removes leftover `fwd_*` files older than 1h; called at the start of every `run()` and once at process startup. |
| 8 | **Orphan heartbeat task** if `_collect_items` raised before the try/finally. | `_collect_items` moved inside the guarded `try` so the `finally` always cancels the heartbeat. |
| 9 | Minor: pipeline config knobs (`DOWNLOAD_MULT`, `UPLOAD_MULT`, `MAX_DL_THREADS`, `MAX_UP_THREADS`) no longer have code consumers. | Left in `config.py` for env-file compatibility; harmless. |

Note on the overlap-vs-serial conflict: `test_download_upload_overlap` asserted parallel pre-uploads
(`upload_ready_order[-1] == 1`). Under the strict serial model that behavior no longer exists; the test was
replaced by `test_download_strict_serial` which asserts the exact serial lifecycle
(`dl, up, dl, up, ...` with no overlap) and that sends land in source order.

### `bot/scheduler.py` (scheduled jobs)
| # | Finding | Fix |
|---|---------|-----|
| 1 | **One shared `TransferEngine` instance across up to 2 concurrent jobs** (`self._sem = Semaphore(2)`). Two `run()` calls mutating the same `_stop/_skip/_paused/_operation/_file_dl` would corrupt each other. | Each job now builds a **fresh `TransferEngine()`** per run. |
| 2 | **Duplicate per-account clients.** `_build_client` created a brand-new `TelegramClient` outside the pool for every job run — if a user-initiated transfer and a scheduled job (or two jobs) hit the same account, two processes shared one session → Telegram "wrong session ID" / "very old message" warnings. | Removed `_build_client`; scheduled jobs now use `client_pool.get(user_id, sid)` — exactly one client per account. |

### `bot/client_pool.py`
| # | Finding | Fix |
|---|---------|-----|
| 1 | **Dead client reuse.** `get()` returned a pooled client even if its connection had dropped; a long-lived process would keep failing on a dead socket. | Added a reconnect guard: `get()` returns the pooled client only when `is_connected()`; otherwise it rebuilds a fresh client (under the per-account lock) and swaps it in. The single-client-per-account invariant is preserved. |

### `bot/main.py`
| # | Finding | Fix |
|---|---------|-----|
| 1 | No `/tmp` hygiene at boot. | `TransferEngine.cleanup_stale_temp()` called after DB init. |

### `tests/test_engine.py`
- `DownloadClient` now records a strict event log (`sequence`: `("dl", id)` / `("up", id)`) so serial ordering is provable.
- `test_download_upload_overlap` → `test_download_strict_serial` (asserts `dl/up` strict alternation, no overlap).
- `test_download_flood_cap`'s `FloodClient` now raises `FloodWaitError` in `send_file` (the engine no longer calls `upload_file`).

## 2. What did NOT change (per constraints)
- UI, bot commands, callback structure, wizard, DB schema, settings, login flow, scheduler UX, progress text, buttons — untouched.
- `Download & Re-upload` still re-groups albums and keeps reply-chain mapping.
- Large single files are still fetched with concurrent part-requests (`_download_media_parallel`), so per-file speed is preserved while only **one file** downloads at a time.
- Retry semantics (`_with_retries`: FloodWait cap, ChatForwardsRestricted fallback to download mode, backoff, `retry_count`, `auto_resume`) unchanged.

## 3. Runtime expectations at ~5k transfers / ~100GB+100GB per day
- RAM: bounded — at most one file in `/tmp` plus one in-flight upload at a time.
- Disk: bounded — temp files deleted per item, startup sweep clears crash leftovers.
- Concurrency: exactly one download and one upload active per account; scheduled and manual runs share the pool.
