# Production-Grade Audit — Telegram Transfer Bot

Audit date: 2026-08-06
Scope: whole `bot/` package + `web.py` + `run.sh` + tests.

The bot runs on Render. Workload target: ~2000 media/day, ~10 GB download and
~10 GB upload per day, 24/7.

## Executive summary

The transfer engine is a **strict sequential pipeline**: one item at a time,
`download -> verify -> upload -> delete temp -> next`. This already satisfies
the "only ONE download / only ONE upload" invariant. It is NOT a multi-queue
architecture, so nothing was rewritten — the sequential loop was hardened.

The freezes the operator sees are caused by **unbounded awaits**. A network
operation (`download_media`, `send_file`, `edit_message`, `get_messages`, ...)
that stops making progress has no timeout, no stall watchdog, and nothing that
detects it. One hung await inside `_process_item` freezes the whole transfer,
progress stops updating, the queue stops, and only a Render redeploy fixes it.

## 1. Critical findings (why the bot freezes)

| # | File:line | Issue | Why it happens |
|---|-----------|-------|----------------|
| C1 | `bot/transfer_engine.py:610`, `:650`, `:834`, `:882`, `:910`, `:940`, `:1014`, `:1038` | **No timeouts on any network op** (`download_media`, `send_file`, `forward_messages`, `send_message`, `get_messages`). | A hung TCP connection / Telegram outage leaves the `await` pending forever. The engine never advances; the queue appears frozen. |
| C2 | `bot/transfer_engine.py:799-800` | **`retry_count == 0` (the UI "Unlimited" option) retries forever.** | `if cfg.retry_count == 0 or attempts <= cfg.retry_count` is always true when `retry_count == 0`, so a permanently-failing message loops forever with capped 10s backoff. The queue is stuck on one item forever. |
| C3 | `bot/transfer_engine.py:652-695` | `_download_media_parallel` part-downloads have **no progress stall watchdog**. | `iter_download` can hang mid-file. No detection → worker freezes. |
| C4 | `bot/handlers/transfer.py:487` | `bot.edit_message` for the live progress is **awaited inline by the engine** (via `progress_cb` -> `render`) with **no timeout**. | If the bot's own Telegram connection hangs, every progress edit hangs. Because the engine awaits `progress_cb` inline (`transfer_engine.py:407/432/437/460`), one hung edit freezes the whole transfer. |
| C5 | `bot/transfer_engine.py:461` | `run()` swallows `asyncio.CancelledError`. | External task cancellation during shutdown is not propagated, leaving cleanup incomplete. |
| C6 | `bot/main.py:198` | `bot.run_until_disconnected()` is called once; an unexpected disconnect ends `main()` and the process. | On Render, a network drop that does not kill the container leaves the bot dead until a manual redeploy. |
| C7 | `bot/client_pool.py:37` | Pooled client is returned whenever `is_connected()` is true — a half-dead socket still counts as connected. | Requests on a dead socket hang (see C1). No proactive sweep of dead clients. |

## 2. Pause/Resume — why it is broken

Pause is **cooperative and only checked between whole items**:

* `bot/transfer_engine.py:247-253` `request_pause` only flips a flag; nothing
  interrupts the current download/upload.
* `bot/transfer_engine.py:422` the only pause check is at the top of the item
  loop, so the current item (download **and** upload) finishes before pausing.
  That is why the user sees "current transfer does not pause / queue
  continues".
* `_sleep_interruptible` (`transfer_engine.py:806`) does not react to pause, so
  retry backoff and FloodWait sleeps ignore it too.
* The Resume button depends on `snap["render"]` editing the message; if that
  edit hangs (C4) or the engine is frozen, the button never changes.
* There is no pause checkpoint *between* the download and the upload of one
  file, so a large file cannot be paused at a safe boundary.

## 3. Worker / queue / task issues

| # | File:line | Issue |
|---|-----------|-------|
| W1 | `transfer_engine.py:499-507` `_collect_items` / `:510` `_fetch_existing` | Batch `get_messages` has no timeout and no retry. A slow/hung fetch skips nothing and can hang the run start. |
| W2 | `transfer_engine.py:269` `collect_ids` | `client.iter_messages` iteration has no per-item timeout; the wizard "Start Transfer" can hang. |
| W3 | `transfer_engine.py:406-411` heartbeat | Orphan risk if `run` is cancelled before `finally`; today `_collect_items` is inside the `try` so this is mostly fixed, but it still pushes progress 2x/sec needlessly. |
| W4 | `transfer_engine.py:714-717` | `db.is_transferred` is awaited outside any try/except; a Mongo stall counts the item as failed (or hangs up to the socket timeout). |
| W5 | `scheduler.py:53-63` | No watchdog on scheduled job tasks; an old hung job could keep its `_executing` entry forever (mitigated once the engine has op timeouts). |
| W6 | `handlers/transfer.py:463` | Each run builds a fresh engine (good). But `transfer.py:29` / `jobs.py:18` / `transfer_engine.py:1114` module singletons are only used for stateless `collect_ids` — harmless but misleading. |

## 4. Session management

* One client per `(user_id, sid)` is enforced by `client_pool.py:42-79` (per-key
  lock + double-check). Good.
* Session is rebuilt when `is_connected()` is false. Good.
* Missing: a dead-socket client that *reports* connected (C7), and no
  engine-level recovery that refreshes the client after repeated network
  failures mid-run.

## 5. Memory / disk

* Temp files are removed in `finally` in `_download_transfer`
  (`transfer_engine.py:1063-1069`). Good.
* `cleanup_stale_temp` runs at boot + per run. Good.
* Orphan tasks: `_download_media_parallel` uses `asyncio.gather` (cleaned up on
  cancel). The handler `update_task` is cancelled in `execute`'s `finally`. OK.
* New risk after decoupling edits: fire-and-forget render tasks must be tracked
  and cancelled or the bot leaks tasks / can write stale progress after the run
  ends.

## 6. Render / process lifecycle

* `web.py` health endpoint always returns 200 even if the bot thread died →
  Render never restarts it. The bot can be dead while health says "OK".
* `main.py` needs an explicit reconnect loop + a bot health watchdog so an
  unexpected disconnect self-heals without a redeploy.

## Fix plan (implemented below)

1. **Operation guards** (`_guard`): every network op is wrapped so it can
   (a) hit a hard timeout, (b) be cancelled by a **progress-stall watchdog** for
   long downloads/uploads, and (c) be interrupted by Pause. No op can hang
   forever.
2. **Retry safety**: per-item retry deadline caps "Unlimited" retries so the
   queue always makes forward progress.
3. **Pause redesign**: dense checkpoints + immediate interruption of the
   current download/upload (cancelled and re-run on resume), preserving overall
   progress and queue position. Resume continues from exactly where Pause
   happened (current file restarts, item is retried).
4. **Progress decoupled from the engine**: renders are fire-and-forget bounded
   tasks with their own timeout; a slow edit can no longer stall a transfer.
5. **Client recovery**: `client_pool.sweep()` for dead clients + engine-level
   `refresh_client` hook that rebuilds the account client after repeated
   network failures.
6. **Main loop**: reconnect-on-disconnect with backoff + bot health watchdog.
7. **web.py**: health endpoint returns 503 when the bot thread is dead so
   Render restarts the process.
8. **Timeouts for the wizard / scheduler / login** so no user-facing path can
   hang forever.
9. **Logging**: worker start/stop, download/upload start/finish, retries,
   pauses, timeouts, watchdogs, reconnects.

No UI text, keyboard layout, callback payload, command, DB schema or settings
changed. Existing features and UX are preserved.
