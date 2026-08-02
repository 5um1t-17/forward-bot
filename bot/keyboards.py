"""Inline keyboard builders. Callback payloads stay well under Telegram's 64-byte limit."""
from __future__ import annotations

from telethon import Button


def main_menu() -> list[list]:
    return [
        [Button.inline("📥 Transfer Messages", b"tr:start")],
        [Button.inline("👤 Accounts", b"acct")],
        [Button.inline("📂 Saved Jobs", b"jobs")],
        [Button.inline("⚙️ Settings", b"set")],
        [Button.inline("📊 Statistics", b"stats")],
    ]


def back_row(cb: str = b"menu") -> list[list]:
    return [[Button.inline("🔙 Back", cb)]]


def accounts_menu_keyboard(accounts: list[dict], active_sid: str | None) -> list[list]:
    kb: list[list] = []
    for acc in accounts:
        mark = "🟢 " if acc["sid"] == active_sid else ""
        label = f"{mark}{acc['name']}"
        kb.append([Button.inline(label, f"acct:sel:{acc['sid']}".encode())])
    kb.append(
        [
            Button.inline("➕ Add Account", b"acct:add"),
            Button.inline("🗑 Delete", b"acct:del"),
        ]
    )
    kb.append([Button.inline("🔙 Back", b"menu")])
    return kb


def accounts_delete_keyboard(accounts: list[dict]) -> list[list]:
    kb: list[list] = []
    for acc in accounts:
        kb.append([Button.inline(f"🗑 {acc['name']} · {acc['phone']}", f"acct:del:{acc['sid']}".encode())])
    kb.append([Button.inline("🔙 Cancel", b"acct")])
    return kb


def source_type_keyboard() -> list[list]:
    return [
        [Button.inline("🔒 Private Group", b"tr:src:pg"),
         Button.inline("🌐 Public Group", b"tr:src:pubg")],
        [Button.inline("🔒 Private Channel", b"tr:src:pc"),
         Button.inline("🌐 Public Channel", b"tr:src:pubc")],
        [Button.inline("✨ Any (auto-detect)", b"tr:src:any")],
        [Button.inline("🔙 Back", b"menu")],
    ]


def source_confirm_keyboard() -> list[list]:
    return [
        [Button.inline("✅ Confirm", b"tr:src:ok"),
         Button.inline("🔁 Change", b"tr:src:again")],
        [Button.inline("🔙 Back", b"menu")],
    ]


def dest_keyboard(dialogs: list[dict], page: int, page_size: int = 6) -> list[list]:
    start = page * page_size
    chunk = dialogs[start:start + page_size]
    kb: list[list] = []
    for d in chunk:
        label = d["title"][:28]
        kb.append([Button.inline(label, f"dst:sel:{d['id']}".encode())])
    row: list = []
    if page > 0:
        row.append(Button.inline("⬅️", f"dst:page:{page - 1}".encode()))
    row.append(Button.inline(f"{page + 1}/{max(1, (len(dialogs) + page_size - 1) // page_size)}", b"dst:noop"))
    if start + page_size < len(dialogs):
        row.append(Button.inline("➡️", f"dst:page:{page + 1}".encode()))
    if row:
        kb.append(row)
    kb.append([Button.inline("✏️ Enter manually", b"dst:manual")])
    kb.append([Button.inline("🔙 Back", b"menu")])
    return kb


def dest_confirm_keyboard() -> list[list]:
    return [
        [Button.inline("✅ Confirm", b"tr:dst:ok"),
         Button.inline("🔁 Change", b"tr:dst:again")],
        [Button.inline("🔙 Back", b"menu")],
    ]


def count_keyboard() -> list[list]:
    return [
        [Button.inline("Latest 10", b"tr:count:10"),
         Button.inline("Latest 50", b"tr:count:50")],
        [Button.inline("Latest 100", b"tr:count:100"),
         Button.inline("Latest 500", b"tr:count:500")],
        [Button.inline("🔗 From Message Link", b"tr:count:link")],
        [Button.inline("🔢 Custom Range", b"tr:count:custom")],
        [Button.inline("🔙 Back", b"menu")],
    ]


def mode_keyboard() -> list[list]:
    return [
        [Button.inline("🔁 Forward Messages", b"tr:mode:forward")],
        [Button.inline("📄 Copy Messages", b"tr:mode:copy")],
        [Button.inline("⬇️ Download & Re-upload", b"tr:mode:download")],
        [Button.inline("🔙 Back", b"menu")],
    ]


def options_keyboard(mode: str, options: set[str]) -> list[list]:
    kb: list[list] = []
    if mode == "forward":
        def mark(key: str) -> str:
            return "✅ " if key in options else "☑️ "
        kb.append([Button.inline(f"{mark('keep_sender')}Keep Original Sender", b"tr:opt:keep_sender")])
        kb.append([Button.inline(f"{mark('hide_header')}Hide Forward Header", b"tr:opt:hide_header")])
        kb.append([Button.inline(f"{mark('remove_captions')}Remove Captions", b"tr:opt:remove_captions")])
    elif mode == "download":
        def mark(key: str) -> str:
            return "✅ " if key in options else "☑️ "
        kb.append([Button.inline(f"{mark('remove_captions')}Remove Captions", b"tr:opt:remove_captions")])
        kb.append([Button.inline(f"{mark('text_only')}Text Only", b"tr:opt:text_only")])
        kb.append([Button.inline(f"{mark('media_only')}Media Only", b"tr:opt:media_only")])
    else:
        def mark(key: str) -> str:
            return "✅ " if key in options else "☑️ "
        kb.append([Button.inline(f"{mark('remove_captions')}Remove Captions", b"tr:opt:remove_captions")])
        kb.append([Button.inline(f"{mark('text_only')}Copy Text Only", b"tr:opt:text_only")])
        kb.append([Button.inline(f"{mark('media_only')}Copy Media Only", b"tr:opt:media_only")])
    kb.append([Button.inline("✅ Continue", b"tr:opt:done")])
    kb.append([Button.inline("🔙 Back", b"menu")])
    return kb


def filter_keyboard(current: str) -> list[list]:
    def mark(key: str) -> str:
        return "✅ " if key == current else "☑️ "
    return [
        [Button.inline(f"{mark('all')}Everything", b"tr:filter:all"),
         Button.inline(f"{mark('photo')}Photos", b"tr:filter:photo")],
        [Button.inline(f"{mark('video')}Videos", b"tr:filter:video"),
         Button.inline(f"{mark('document')}Documents", b"tr:filter:document")],
        [Button.inline(f"{mark('text')}Text", b"tr:filter:text"),
         Button.inline(f"{mark('media')}Media", b"tr:filter:media")],
        [Button.inline("✅ Continue", b"tr:filter:done")],
        [Button.inline("🔙 Back", b"menu")],
    ]


def dedup_keyboard(enabled: bool) -> list[list]:
    state = "✅ ON" if enabled else "❌ OFF"
    return [
        [Button.inline(state, b"tr:dedup:toggle")],
        [Button.inline("✅ Continue", b"tr:dedup:done")],
        [Button.inline("🔙 Back", b"menu")],
    ]


def schedule_keyboard() -> list[list]:
    return [
        [Button.inline("🚀 Run Now", b"tr:sched:now")],
        [Button.inline("🕒 Schedule Later", b"tr:sched:later")],
        [Button.inline("📅 Daily", b"tr:sched:daily")],
        [Button.inline("🗓 Weekly", b"tr:sched:weekly")],
        [Button.inline("🔙 Back", b"menu")],
    ]


def weekday_keyboard() -> list[list]:
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    row: list = []
    kb: list[list] = []
    for i, d in enumerate(days):
        row.append(Button.inline(d, f"tr:sched:wd:{i}".encode()))
        if len(row) == 4:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    return kb


def summary_keyboard() -> list[list]:
    return [
        [Button.inline("🚀 Start Transfer", b"tr:run:start"),
         Button.inline("💾 Save as Job", b"tr:run:savejob")],
        [Button.inline("✏️ Edit", b"tr:run:edit")],
        [Button.inline("🔙 Cancel", b"menu")],
    ]


def edit_keyboard() -> list[list]:
    return [
        [Button.inline("1️⃣ Source", b"tr:edit:src"), Button.inline("2️⃣ Destination", b"tr:edit:dst")],
        [Button.inline("3️⃣ Count", b"tr:edit:count"), Button.inline("4️⃣ Mode", b"tr:edit:mode")],
        [Button.inline("5️⃣ Options", b"tr:edit:opts"), Button.inline("6️⃣ Filter", b"tr:edit:filter")],
        [Button.inline("7️⃣ Dedup", b"tr:edit:dedup"), Button.inline("8️⃣ Schedule", b"tr:edit:sched")],
        [Button.inline("🔙 Back", b"menu")],
    ]


def running_keyboard() -> list[list]:
    return [
        [Button.inline("🔄 Refresh", b"tr:run:refresh"),
         Button.inline("🛑 Stop", b"tr:run:stop")],
    ]


def run_done_keyboard() -> list[list]:
    return [
        [Button.inline("💾 Save as Job", b"tr:run:savejob")],
        [Button.inline("🔙 Menu", b"menu")],
    ]


def jobs_menu_keyboard(jobs: list[dict]) -> list[list]:
    kb: list[list] = []
    for j in jobs:
        name = j.get("name", "Unnamed")[:28]
        kb.append([Button.inline(f"🚀 {name}", f"jobs:run:{str(j['_id'])}".encode())])
    kb.append([Button.inline("🔙 Back", b"menu")])
    return kb


def job_info_keyboard(jid: str) -> list[list]:
    return [
        [Button.inline("🚀 Run Now", f"jobs:run:{jid}".encode())],
        [Button.inline("🗑 Delete", f"jobs:del:{jid}".encode())],
        [Button.inline("🔙 Back", b"jobs")],
    ]


def job_confirm_delete_keyboard(jid: str) -> list[list]:
    return [
        [Button.inline("🗑 Yes, delete", f"jobs:del2:{jid}".encode())],
        [Button.inline("🔙 Cancel", b"jobs")],
    ]


def settings_keyboard(s: dict) -> list[list]:
    def onoff(key: str) -> str:
        return "ON" if s.get(key) else "OFF"
    return [
        [Button.inline(f"🔁 Delay: {s['forward_delay']}s", b"set:delay")],
        [Button.inline(f"🧵 Threads: {s['threads']}", b"set:threads")],
        [Button.inline(f"🔃 Retry: {s['retry_count']}", b"set:retry")],
        [Button.inline(f"🌊 FloodWait: {onoff('handle_flood')}", b"set:flood")],
        [Button.inline(f"♻️ Auto Resume: {onoff('auto_resume')}", b"set:resume")],
        [Button.inline(f"🔔 Notifications: {onoff('notifications')}", b"set:notif")],
        [Button.inline(f"🌙 Dark Theme: {onoff('dark_theme')}", b"set:theme")],
        [Button.inline("🔙 Back", b"menu")],
    ]


def settings_choice_keyboard(prefix: str, items: list[tuple[str, str]]) -> list[list]:
    kb: list[list] = []
    for value, label in items:
        kb.append([Button.inline(label, f"set:{prefix}:{value}".encode())])
    kb.append([Button.inline("🔙 Back", b"set")])
    return kb


def stats_keyboard(is_admin: bool) -> list[list]:
    kb: list[list] = []
    if is_admin:
        kb.append([Button.inline("🌍 Global Stats", b"stats:global")])
    kb.append([Button.inline("🔙 Back", b"menu")])
    return kb
