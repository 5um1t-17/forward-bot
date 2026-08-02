"""Centralised UI copy for the bot."""
from __future__ import annotations

from bot.config import config

T = {
    "emoji": {
        "transfer": "📥",
        "accounts": "👤",
        "jobs": "📂",
        "settings": "⚙️",
        "stats": "📊",
        "back": "🔙",
        "next": "▶️",
        "ok": "✅",
        "cancel": "❌",
        "save": "💾",
        "run": "🚀",
        "delete": "🗑",
        "lock": "🔒",
        "public": "🌐",
        "clock": "🕒",
        "bolt": "⚡",
        "warn": "⚠️",
    }
}


def menu_text(user: dict | None) -> str:
    name = (user or {}).get("first_name", "there")
    return (
        f"👋 Welcome, <b>{_escape(name)}</b>!\n\n"
        "I transfer messages between your Telegram groups & channels "
        "using Telegram's fast server-side forwarding — no media is ever "
        "downloaded or re-uploaded.\n\n"
        "Choose an option below."
    )


def accounts_menu(accounts: list[dict], active_sid: str | None) -> str:
    if not accounts:
        return (
            "👤 <b>Accounts</b>\n\n"
            "No accounts added yet.\n\n"
            "Add your first Telegram account to start transferring."
        )
    lines = ["👤 <b>Accounts</b>\n"]
    for acc in accounts:
        badge = "🟢" if acc["sid"] == active_sid else "⚪"
        lines.append(f"{badge} <b>{_escape(acc['name'])}</b> · {acc['phone']}")
    lines.append("\nTap the active account to switch.")
    return "\n".join(lines)


def add_account_step1() -> str:
    return (
        "🔐 <b>Add Telegram Account</b>\n\n"
        "Send the account's <b>phone number</b> in international format.\n"
        "Example: <code>+15551234567</code>\n\n"
        "Use /cancel to abort."
    )


def add_account_step2() -> str:
    return (
        "📨 <b>Login code sent!</b>\n\n"
        "Enter the 5-digit code you received on Telegram (or via SMS).\n\n"
        "Use /cancel to abort."
    )


def add_account_step3() -> str:
    return (
        "🔑 <b>Two-Factor Authentication</b>\n\n"
        "This account has 2FA enabled. Enter your password.\n\n"
        "Use /cancel to abort."
    )


def login_success(name: str, phone: str) -> str:
    return (
        f"✅ <b>Account added!</b>\n\n"
        f"Name: <b>{_escape(name)}</b>\n"
        f"Phone: <code>{phone}</code>\n\n"
        "Your session is stored <b>encrypted</b>."
    )


def source_prompt() -> str:
    return (
        "📥 <b>Step 1 — Source</b>\n\n"
        "Where should messages come from? Send one of the following:\n\n"
        "• A message <b>link</b>: <code>https://t.me/username/123</code>\n"
        "• A <b>username</b>: <code>@mychannel</code>\n"
        "• A numeric <b>chat ID</b>: <code>-1001234567890</code>\n"
        "• <b>Forward any message</b> from the source chat to me\n\n"
        "Type /cancel to abort."
    )


def source_confirmed(name: str, chat_id: int) -> str:
    return (
        f"✅ <b>Source set</b>\n\n"
        f"Name: <b>{_escape(name)}</b>\n"
        f"ID: <code>{chat_id}</code>\n"
        f"Type: <code>{_kind(name)}</code>"
    )


def dest_prompt() -> str:
    return (
        "📥 <b>Step 2 — Destination</b>\n\n"
        "Where should the messages be transferred to?\n"
        "Pick a group/channel below, or enter a link manually."
    )


def dest_confirmed(name: str, chat_id: int) -> str:
    return (
        f"✅ <b>Destination set</b>\n\n"
        f"Name: <b>{_escape(name)}</b>\n"
        f"ID: <code>{chat_id}</code>"
    )


def count_prompt() -> str:
    return "📥 <b>Step 3 — How many messages?</b>\n\nChoose a preset or a custom message-ID range."


def custom_start_prompt() -> str:
    return (
        "🔢 <b>Custom Range</b>\n\n"
        "Enter the <b>start message ID</b> (smallest):\n\n"
        "Send a single number.\n\n"
        "Use /cancel to abort."
    )


def custom_end_prompt() -> str:
    return (
        "🔢 <b>Custom Range</b>\n\n"
        "Enter the <b>end message ID</b> (largest):\n\n"
        "Send a single number.\n\n"
        "Use /cancel to abort."
    )


def link_start_prompt() -> str:
    return (
        "🔗 <b>From Message Link — Step 1/2</b>\n\n"
        "Send the <b>start message link</b> from the source chat.\n\n"
        "Example: <code>https://t.me/channel/123</code> or <code>https://t.me/joinchat/abc123/123</code>\n\n"
        "Use /cancel to abort."
    )


def link_end_prompt() -> str:
    return (
        "🔗 <b>From Message Link — Step 2/2</b>\n\n"
        "Now send the <b>end message link</b> from the same source chat.\n\n"
        "Example: <code>https://t.me/channel/500</code> or <code>https://t.me/joinchat/abc123/500</code>\n\n"
        "All messages from the start to the end will be transferred.\n\n"
        "Use /cancel to abort."
    )


def mode_prompt() -> str:
    return (
        "📥 <b>Step 4 — Transfer Mode</b>\n\n"
        "🔁 <b>Forward</b> — uses Telegram's native forwarding; keeps the "
        "'forwarded from' tag and original sender.\n\n"
        "📄 <b>Copy</b> — copies the message so the forwarded tag is removed. "
        "Media stays on Telegram servers (no re-upload).\n\n"
        "⬇️ <b>Download</b> — downloads media and re-uploads it. Works even when "
        "forwarding/copying is restricted in private groups."
    )


def options_prompt(mode: str, options: set[str]) -> str:
    head = "📥 <b>Step 5 — Options</b>\n\n"
    if mode == "forward":
        head += "Forwarding options:\n"
    elif mode == "download":
        head += "Download & Re-upload options:\n"
    else:
        head += "Copy options:\n"

    def row(key: str, label: str) -> str:
        mark = "✅" if key in options else "☑️"
        return f"{mark} {label}"

    lines = []
    if mode == "forward":
        lines.append(row("keep_sender", "Keep Original Sender"))
        lines.append(row("hide_header", "Hide Forward Header"))
        lines.append(row("remove_captions", "Remove Captions"))
    elif mode == "download":
        lines.append(row("remove_captions", "Remove Captions"))
        lines.append(row("text_only", "Text Only"))
        lines.append(row("media_only", "Media Only"))
    else:
        lines.append(row("hide_header", "Hide Forward Header"))
        lines.append(row("remove_captions", "Remove Captions"))
        lines.append(row("text_only", "Copy Text Only"))
        lines.append(row("media_only", "Copy Media Only"))
    return head + "\n".join(lines) + "\n\nPress <b>Continue</b> when done."


def filter_prompt() -> str:
    return "📥 <b>Step 6 — What to transfer</b>\n\nChoose which message types to include:"


def dedup_prompt(enabled: bool) -> str:
    state = "ON" if enabled else "OFF"
    return (
        "📥 <b>Step 7 — Duplicate Detection</b>\n\n"
        f"Skip already-transferred messages: <b>{state}</b>\n\n"
        "Transferred message IDs are stored in MongoDB so re-runs skip them."
    )


def schedule_prompt() -> str:
    return (
        "📥 <b>Step 8 — Schedule</b>\n\n"
        "Run the transfer now, or schedule it for later."
    )


def schedule_time_prompt() -> str:
    return (
        "🕒 <b>Schedule Time</b>\n\n"
        "Enter the time in <code>HH:MM</code> format (24h).\n\n"
        "Example: <code>21:30</code>\n\n"
        "Use /cancel to abort."
    )


def schedule_weekday_prompt() -> str:
    return "📅 <b>Schedule Weekday</b>\n\nChoose the day for the weekly job:"


def summary(cfg: dict, src: dict, dst: dict) -> str:
    options = cfg.get("options", set())
    opt_lines = []
    if "keep_sender" in options:
        opt_lines.append("• Keep original sender")
    if "hide_header" in options:
        opt_lines.append("• Hide forward header")
    if "remove_captions" in options:
        opt_lines.append("• Remove captions")
    if "text_only" in options:
        opt_lines.append("• Text only")
    if "media_only" in options:
        opt_lines.append("• Media only")
    if not opt_lines:
        opt_lines.append("• Default")

    schedule = cfg.get("schedule_kind", "now")
    sched_txt = {"now": "Run now", "later": "Later", "daily": "Daily", "weekly": "Weekly"}[schedule]
    if schedule in ("later", "daily", "weekly"):
        sched_txt += f" @ {cfg.get('schedule_time', '?')}"
        if schedule == "weekly":
            sched_txt += f" ({cfg.get('schedule_weekday', '?')})"

    mode_txt = "🔁 Forward" if cfg.get("mode") == "forward" else ("📄 Copy" if cfg.get("mode") == "copy" else "⬇️ Download")

    return (
        "📋 <b>Transfer Summary</b>\n\n"
        f"<b>Source:</b> {_escape(src['name'])}\n"
        f"<b>Destination:</b> {_escape(dst['name'])}\n"
        f"<b>Messages:</b> {cfg.get('count_label', '?')}\n"
        f"<b>Mode:</b> {mode_txt}\n"
        f"<b>Filter:</b> {cfg.get('filter_label', 'Everything')}\n"
        f"<b>Options:</b>\n" + "\n".join(opt_lines) + "\n"
        f"<b>Dedup:</b> {'ON' if cfg.get('dedup') else 'OFF'}\n"
        f"<b>Schedule:</b> {sched_txt}\n\n"
        "Ready to start?"
    )


def _bar(fraction: float, width: int = 12) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return "█" * filled + "░" * (width - filled)


def progress_text(done: int, total: int, elapsed: float, speed: float,
                  skipped: int, failed: int, mode: str, dark: bool = False) -> str:
    fraction = done / total if total else 0.0
    if mode == "forward":
        verb = "Forwarding"
    elif mode == "download":
        verb = "Downloading"
    else:
        verb = "Copying"
    icon = "🌙" if dark else "☀️"
    lines = [
        f"{icon} <b>{verb}...</b>",
        "",
        f"<code>{_bar(fraction)}</code>",
        f"<b>{fraction * 100:.1f}%</b>",
        f"<b>{done}</b> / {total}",
        "",
        f"Elapsed: <code>{_fmt_elapsed(elapsed)}</code>",
        f"Speed: <code>{speed:.1f}</code> msg/sec",
        f"Skipped: <code>{skipped}</code>",
        f"Failed: <code>{failed}</code>",
    ]
    return "\n".join(lines)


def run_done(summary_: dict, duration: float) -> str:
    return (
        "🎉 <b>Transfer complete!</b>\n\n"
        f"Total: <b>{summary_['total']}</b>\n"
        f"✅ Success: <b>{summary_['success']}</b>\n"
        f"⏭ Skipped: <b>{summary_['skipped']}</b>\n"
        f"❌ Failed: <b>{summary_['failed']}</b>\n"
        f"⏱ Duration: <code>{_fmt_elapsed(duration)}</code>"
    )


def run_failed(summary_: dict, duration: float, reason: str) -> str:
    return (
        "🛑 <b>Transfer interrupted</b>\n\n"
        f"Reason: <code>{_escape(reason)}</code>\n\n"
        f"Success: <b>{summary_['success']}</b>\n"
        f"Skipped: <b>{summary_['skipped']}</b>\n"
        f"Failed: <b>{summary_['failed']}</b>\n"
        f"⏱ Duration: <code>{_fmt_elapsed(duration)}</code>"
    )


def jobs_menu(jobs: list[dict]) -> str:
    if not jobs:
        return "📂 <b>Saved Jobs</b>\n\nNo saved jobs yet.\n\nFinish a transfer and use <b>Save as Job</b>."
    lines = ["📂 <b>Saved Jobs</b>\n"]
    for j in jobs:
        name = j.get("name", "Unnamed")
        kind = j.get("schedule_kind", "now")
        status = j.get("status", "saved")
        sched = {"now": "one-shot", "later": "later", "daily": "daily", "weekly": "weekly"}[kind]
        lines.append(f"• <b>{_escape(name)}</b> — {sched} [{status}]")
    return "\n".join(lines)


def job_saved(name: str, jid: str) -> str:
    return (
        f"💾 Job <b>{_escape(name)}</b> saved.\n\n"
        f"Job ID: <code>{jid}</code>\n"
        "You can run it again anytime from 📂 Saved Jobs."
    )


def settings_menu(s: dict) -> str:
    return (
        "⚙️ <b>Settings</b>\n\n"
        f"🔁 Forward Delay: <b>{s['forward_delay']}s</b>\n"
        f"🧵 Threads: <b>{s['threads']}</b>\n"
        f"🔃 Retry: <b>{'unlimited' if s['retry_count'] == 0 else str(s['retry_count']) + 'x'}</b>\n"
        f"🌊 FloodWait: <b>{'on' if s['handle_flood'] else 'off'}</b>\n"
        f"♻️ Auto Resume: <b>{'on' if s['auto_resume'] else 'off'}</b>\n"
        f"🔔 Notifications: <b>{'on' if s['notifications'] else 'off'}</b>\n"
        f"🌙 Dark Theme: <b>{'on' if s['dark_theme'] else 'off'}</b>"
    )


def stats_text(user: dict, settings_: dict, per_mode: list[dict], total: int,
               today: int, recent: list[dict]) -> str:
    is_admin = user["user_id"] in config.ADMIN_IDS
    lines = [
        "📊 <b>Statistics</b>\n",
        f"Total transferred: <b>{total}</b>",
        f"Transferred (24h): <b>{today}</b>",
        f"Threads: {settings_['threads']} · Retry: {settings_['retry_count']}",
        "",
        "By mode:",
    ]
    if per_mode:
        for m in per_mode:
            mode_id = m.get("_id", "")
            if mode_id == "forward":
                label = "🔁 Forward"
            elif mode_id == "download":
                label = "⬇️ Download"
            else:
                label = "📄 Copy"
            lines.append(f"  {label}: <b>{m['count']}</b>")
    else:
        lines.append("  (none yet)")
    if is_admin:
        lines.append("\n<i>Admin view enabled — use the Global Stats button.</i>")
    if recent:
        lines.append("\nRecent runs:")
        for r in recent[:3]:
            src = (r.get("source_name") or "?")[:20]
            dst = (r.get("dest_name") or "?")[:20]
            lines.append(
                f"  • {src} → {dst}: "
                f"{r.get('success', 0)}/{r.get('total', 0)} ok"
            )
    return "\n".join(lines)


def admin_stats(total_users: int, total_accounts: int, total_msgs: int,
                per_account: list[dict], sessions: list[dict]) -> str:
    lines = [
        "📊 <b>Global Statistics (Admin)</b>\n",
        f"Users: <b>{total_users}</b>",
        f"Accounts: <b>{total_accounts}</b>",
        f"Messages transferred: <b>{total_msgs}</b>",
        "",
        "Top accounts:",
    ]
    phone_map = {s["sid"]: s.get("phone", "?") for s in sessions}
    if per_account:
        for entry in per_account:
            lines.append(f"  • {phone_map.get(entry['_id'], '?')}: <b>{entry['count']}</b> msgs")
    else:
        lines.append("  (none yet)")
    return "\n".join(lines)


def err_no_account() -> str:
    return "⚠️ You need an account first. Please add one under 👤 Accounts."


def err_invalid_input() -> str:
    return "⚠️ Sorry, I didn't understand that. Please try again."


def commands_help() -> str:
    return (
        "⌨️ <b>Fast-access commands</b>\n\n"
        "/start — Main menu\n"
        "/transfer — Start a transfer\n"
        "/accounts — Manage accounts\n"
        "/jobs — Saved jobs\n"
        "/settings — Settings\n"
        "/stats — Statistics\n"
        "/cleanup — Reset dedup records\n"
        "/cancel — Abort the current step"
    )


# ----------------------------------------------------------------------
def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _kind(name: str) -> str:
    return "chat"


def _fmt_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
