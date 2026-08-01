# Telegram Message Transfer Bot

A professional Telegram bot that copies old messages from one private/public
group or channel to another **without downloading or re-uploading media**. It
uses Telegram's native server-side forwarding / copying APIs, so only message
IDs travel over the network — transfers are extremely fast and use minimal
bandwidth.

Built with **Python**, **Telethon**, **MongoDB** (async via motor) and an
inline-keyboard admin interface.

---

## Features

| Area | Details |
|---|---|
| **Login** | Add Telegram accounts with phone + code (+ 2FA password). Sessions are encrypted with Fernet before being stored in MongoDB. Multiple accounts per user, instant switching. |
| **Main menu** | Transfer Messages, Accounts, Saved Jobs, Settings, Statistics. |
| **Source** | Auto-detect from a message link (`t.me/username/123`), username (`@x` / `x`), numeric chat ID, `t.me/c/<id>/<msg>` private-channel link, or by forwarding any message from the source chat to the bot. |
| **Destination** | Browse sendable groups/channels (paginated inline keyboard) or enter a link / username / ID manually. |
| **Count** | Latest 10 / 50 / 100 / 500, or a custom inclusive message-ID range. |
| **Mode** | **Forward** (native `forward_messages`, keeps original sender) or **Copy** (`send_file`/`send_message` with existing input media — forwarded tag removed, still no re-upload). |
| **Options** | Hide forward header, keep original sender, keep/remove captions, copy text only, copy media only. Invalid combinations are enforced automatically. |
| **Media** | Photos, videos, documents, voice, audio, GIFs, stickers, polls, contacts, albums (albums stay albums), replies (best-effort remap). |
| **Filters** | Everything, Photos, Videos, Documents, Text, Media. |
| **Dedup** | "Skip already copied" — transferred IDs stored in `transferred_messages`, re-runs skip them. |
| **Speed** | No media download. `threads` concurrent workers drain an asyncio queue (Telegram limits respected). |
| **Progress** | Live progress bar edited in place: `████████░░░░░░`, done/total, elapsed, msg/sec, skipped, failed. Stop button included. |
| **FloodWait** | `FloodWaitError` is detected, slept through and processing resumes automatically. |
| **Retry** | Auto-retry failed messages (3x / 5x / unlimited, per user setting). |
| **Scheduling** | Run now, schedule later (one-off), daily, or weekly. |
| **Saved Jobs** | Save any transfer config and re-run it with one tap. |
| **Settings** | Forward delay, threads, retry count, FloodWait handling, auto resume, notifications, dark theme. |
| **Statistics** | Per-user totals, last-24h, mode breakdown, recent runs. Admins get global stats. |
| **Security** | Encrypted sessions, no API credentials in code (`.env`), role-based admin access, no secrets committed. |

---

## Tech stack

- Python 3.11+, `asyncio`
- Telethon (MTProto client)
- MongoDB via `motor` (async) + `pymongo`
- `cryptography` (Fernet) for session encryption
- `python-dotenv` for configuration

---

## Project structure

```
.
├── bot/
│   ├── main.py               # entry point, event dispatcher, scheduler bootstrap
│   ├── config.py             # environment configuration
│   ├── crypto.py             # Fernet session encryption
│   ├── db.py                 # MongoDB repositories (users, sessions, jobs, logs,
│   │                         #   settings, transferred_messages)
│   ├── state.py              # in-memory conversation state machine
│   ├── session_manager.py    # account CRUD + StringSession build/decrypt
│   ├── client_pool.py        # pooled user TelegramClient instances
│   ├── entity_resolver.py    # links / usernames / IDs / forwarded-message resolution
│   ├── transfer_engine.py    # forward/copy engine: queue, concurrency, FloodWait,
│   │                         #   dedup, albums, replies, progress callbacks
│   ├── scheduler.py          # due-job runner for later/daily/weekly schedules
│   ├── keyboards.py          # inline keyboard builders
│   ├── text.py               # all UI copy
│   ├── logger.py             # rotating file + console logging
│   └── handlers/
│       ├── start.py          # /start + main menu
│       ├── accounts.py       # add / switch / delete accounts (login flow)
│       ├── transfer.py       # transfer wizard (8 steps) + live run
│       ├── jobs.py           # saved jobs list / run / delete
│       ├── settings.py       # per-user settings
│       └── stats.py          # statistics (incl. admin global view)
├── tests/                    # offline smoke/integration tests (no live API)
├── requirements.txt
├── .env.example
└── run.sh
```

---

## Setup

### 1. Prerequisites

- Python 3.11+
- MongoDB (local or remote)
- Telegram API credentials from https://my.telegram.org/apps (`api_id`, `api_hash`)
- A bot token from [@BotFather](https://t.me/BotFather)

### 2. Install

```bash
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# edit .env
```

Required variables in `.env`:

```
API_ID=123456
API_HASH=your_api_hash
BOT_TOKEN=123456:ABC...           # from @BotFather
MONGO_URI=mongodb://127.0.0.1:27017
MONGO_DB=telegram_transfer_bot
ADMIN_IDS=111111,222222           # numeric Telegram user IDs (optional)
```

`SESSION_ENCRYPTION_KEY` is optional — if empty, a Fernet key is generated on
first run and stored in `session.key` (mode `0600`).

### 4. Run

```bash
./run.sh
# or
python3 -m bot.main
```

---

## Usage

1. Start the bot (`/start`), open **👤 Accounts → ➕ Add Account**, and sign in
   with your Telegram account (phone → code → optional 2FA password).
2. **📥 Transfer Messages** and walk through the wizard:

   | Step | Input |
   |------|-------|
   | 1. Source | send a link / username / ID / forwarded message |
   | 2. Destination | pick from the dialog list or enter manually |
   | 3. Count | Latest 10/50/100/500 or custom message-ID range |
   | 4. Mode | Forward or Copy |
   | 5. Options | toggle per-mode options |
   | 6. Filter | message types to include |
   | 7. Dedup | skip already-copied messages |
   | 8. Schedule | run now / later / daily / weekly |

3. Press **🚀 Start Transfer** and watch the live progress. Use **🛑 Stop**
   to abort at any time.
4. Press **💾 Save as Job** to re-run the same transfer later from **📂 Saved Jobs**.

---

## How media stays on Telegram servers

- **Forward mode** → `client.forward_messages(dest, ids, from_peer=source)`.
- **Copy mode** → `client.send_file(dest, message.media, ...)` / 
  `client.send_message(dest, message, ...)`. Passing the *existing*
  `InputMedia` (photo/document) makes Telethon issue a
  `messages.SendMediaRequest` that references media already on Telegram's CDN —
  nothing is ever downloaded or re-uploaded.

---

## Tests

Offline tests use fake clients and a local MongoDB — no live Telegram API is
needed:

```bash
PYTHONPATH=. python3 tests/test_engine.py        # engine, resolver, filters, dedup, stop
PYTHONPATH=. python3 tests/test_scheduler.py     # scheduled jobs end-to-end
PYTHONPATH=. python3 tests/test_accounts.py      # login flow + encrypted storage
PYTHONPATH=. python3 tests/test_integration.py   # full wizard + run + save job
```

---

## Security notes

- Telethon session strings are encrypted with Fernet before persisting to
  MongoDB; the key lives only in the environment / `session.key`.
- API credentials come exclusively from environment variables — never hardcode.
- Bot admins are defined by `ADMIN_IDS`; the global statistics view is
  admin-only.
- Do not share `.env` or `session.key`; both are gitignored.

---

## Notes & limitations

- Forwarding does not preserve reply chains (Telegram API limitation); Copy
  mode remaps replies best-effort when the parent was already copied.
- "Latest N" collects the N most recent messages that pass the active filter
  (scanning backwards, capped for safety).
- Custom ranges are capped at 20,000 IDs by default (`MAX_CUSTOM_RANGE`).
- Service messages are always skipped.
