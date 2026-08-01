"""Transfer wizard: source -> destination -> count -> mode -> options ->
filter -> dedup -> schedule -> run / save as job."""
from __future__ import annotations

import asyncio
import logging

from telethon import events
from telethon.errors import MessageNotModifiedError, MessageIdInvalidError

from bot import keyboards, text
from bot.client_pool import client_pool
from bot.db import db, now
from bot.entity_resolver import fetch_sendable_dialogs, parse_input, resolve, resolve_forwarded, _resolve_private_channel
from bot.handlers.common import answer, edit
from bot.scheduler import _compute_next, schedule_instant
from bot.state import TransferWizard, store
from bot.transfer_engine import (
    TransferConfig,
    TransferEngine,
    TransferResult,
    filter_label,
)

log = logging.getLogger("bot.transfer")

engine = TransferEngine()

_ACTIONS = {"tr"}


async def handle(bot, event: events.CallbackQuery.Event, data: str) -> bool:
    if data.startswith(("tr:", "dst:")) or data == "tr":
        return await _route(bot, event, data)
    return False


async def _route(bot, event, data: str) -> bool:
    uid = event.sender_id
    if data == "tr:start":
        return await _start_wizard(bot, event, uid)
    if data == "tr:src:ok":
        return await _next_to_dest(bot, event, uid)
    if data == "tr:src:again":
        return await _ask_source(bot, event, uid)
    if data.startswith("tr:src:"):
        return await _on_source_type(bot, event, uid, data.split(":", 2)[2])
    if data.startswith("dst:sel:"):
        return await _on_dest_sel(bot, event, uid, int(data.split(":", 2)[2]))
    if data.startswith("dst:page:"):
        return await _on_dest_page(bot, event, uid, int(data.split(":", 2)[2]))
    if data == "dst:manual":
        return await _ask_dest_manual(bot, event, uid)
    if data == "dst:noop":
        await answer(event)
        return True
    if data == "tr:dst:ok":
        return await _ask_count(bot, event, uid)
    if data == "tr:dst:again":
        return await _ask_dest(bot, event, uid)
    if data.startswith("tr:count:"):
        return await _on_count(bot, event, uid, data.split(":", 2)[2])
    if data.startswith("tr:mode:"):
        return await _on_mode(bot, event, uid, data.split(":", 2)[2])
    if data == "tr:opt:done":
        return await _ask_filter(bot, event, uid)
    if data.startswith("tr:opt:"):
        return await _on_option(bot, event, uid, data.split(":", 2)[2])
    if data == "tr:filter:done":
        return await _ask_dedup(bot, event, uid)
    if data.startswith("tr:filter:"):
        return await _on_filter(bot, event, uid, data.split(":", 2)[2])
    if data == "tr:dedup:toggle":
        wiz = store.get_transfer(uid)
        wiz.dedup = not wiz.dedup
        await edit(event, text.dedup_prompt(wiz.dedup), keyboards.dedup_keyboard(wiz.dedup))
        return True
    if data == "tr:dedup:done":
        return await _ask_schedule(bot, event, uid)
    if data.startswith("tr:sched:"):
        return await _on_schedule(bot, event, uid, data.split(":", 2)[2])
    if data == "tr:run:start":
        return await _run(bot, event, uid)
    if data == "tr:run:stop":
        return await _stop(bot, event, uid)
    if data == "tr:run:savejob":
        return await _ask_job_name(bot, event, uid)
    if data == "tr:run:edit":
        await edit(event, "✏️ Which step would you like to change?", keyboards.edit_keyboard())
        return True
    if data.startswith("tr:edit:"):
        return await _edit_step(bot, event, uid, data.split(":", 2)[2])
    return False


async def _start_wizard(bot, event, uid: int) -> bool:
    sid = await db.get_active_sid(uid)
    if not sid:
        await answer(event, text.err_no_account(), alert=True)
        return True
    store.reset_transfer(uid)
    wiz = store.get_transfer(uid)
    wiz.step = "source_type"
    await edit(
        event,
        "📥 <b>Transfer Messages</b>\n\nWhat kind of source chat is it?\n"
        "(This is used for validation only — auto-detect works too.)",
        keyboards.source_type_keyboard(),
    )
    return True


async def _on_source_type(bot, event, uid: int, stype: str) -> bool:
    wiz = store.get_transfer(uid)
    wiz.source_type = stype
    return await _ask_source(bot, event, uid)


async def _ask_source(bot, event, uid: int) -> bool:
    store.set_pending(uid, "tr_source")
    await edit(event, text.source_prompt(), keyboards.back_row())
    return True


async def _ask_dest_manual(bot, event, uid: int) -> bool:
    store.set_pending(uid, "tr_dest_manual")
    await edit(event, text.dest_prompt() + "\n\nSend the destination <b>link</b>, <b>username</b> or <b>ID</b> manually:", keyboards.back_row())
    return True


async def _on_dest_sel(bot, event, uid: int, dialog_id: int) -> bool:
    wiz = store.get_transfer(uid)
    for d in wiz.dialogs:
        if d["id"] == dialog_id:
            wiz.dest = {"id": dialog_id, "name": d["title"]}
            await edit(event, text.dest_confirmed(d["title"], dialog_id), keyboards.dest_confirm_keyboard())
            return True
    await answer(event, "Dialog not found", alert=True)
    return True


async def _on_dest_page(bot, event, uid: int, page: int) -> bool:
    wiz = store.get_transfer(uid)
    wiz.dest_page = page
    if not wiz.dialogs:
        return await _ask_dest(bot, event, uid)
    await edit(event, text.dest_prompt(), keyboards.dest_keyboard(wiz.dialogs, page))
    return True


async def _next_to_dest(bot, event, uid: int) -> bool:
    return await _ask_dest(bot, event, uid)


async def _ask_dest(bot, event, uid: int) -> bool:
    wiz = store.get_transfer(uid)
    store.set_pending(uid, None)
    wiz.dest_page = 0
    await edit(event, "⏳ Loading destination chats...", None)
    try:
        sid = await db.get_active_sid(uid)
        client = await client_pool.get(uid, sid)
        wiz.dialogs = await fetch_sendable_dialogs(client)
    except Exception as exc:
        log.warning("dialogs failed: %s", exc)
        await edit(event, f"⚠️ Could not load your chats:\n<code>{str(exc)[:200]}</code>", keyboards.back_row())
        return True
    if not wiz.dialogs:
        await edit(event, "⚠️ No groups/channels found where you can post.", keyboards.back_row())
        return True
    await edit(event, text.dest_prompt(), keyboards.dest_keyboard(wiz.dialogs, 0))
    return True


async def _ask_count(bot, event, uid: int) -> bool:
    store.set_pending(uid, None)
    await edit(event, text.count_prompt(), keyboards.count_keyboard())
    return True


async def _on_count(bot, event, uid: int, value: str) -> bool:
    wiz = store.get_transfer(uid)
    if value == "custom":
        store.set_pending(uid, "tr_start_id")
        await edit(event, text.custom_start_prompt(), keyboards.back_row())
        return True
    if value == "link":
        store.set_pending(uid, "tr_link_start")
        await edit(event, text.link_start_prompt(), keyboards.back_row())
        return True
    wiz.count_mode = "latest"
    wiz.count = int(value)
    return await _ask_mode(bot, event, uid)


async def _ask_mode(bot, event, uid: int) -> bool:
    store.set_pending(uid, None)
    await edit(event, text.mode_prompt(), keyboards.mode_keyboard())
    return True


async def _on_mode(bot, event, uid: int, mode: str) -> bool:
    wiz = store.get_transfer(uid)
    wiz.mode = mode
    if mode == "forward":
        wiz.options.discard("hide_header")
        wiz.options.add("keep_sender")
        wiz.options.discard("text_only")
        wiz.options.discard("media_only")
    elif mode == "download":
        wiz.options.discard("keep_sender")
        wiz.options.discard("hide_header")
        wiz.options.discard("text_only")
        wiz.options.discard("media_only")
    else:
        wiz.options.discard("keep_sender")
    await edit(event, text.options_prompt(mode, wiz.options), keyboards.options_keyboard(mode, wiz.options))
    return True


async def _on_option(bot, event, uid: int, key: str) -> bool:
    wiz = store.get_transfer(uid)
    opts = wiz.options
    if key == "keep_sender":
        if "keep_sender" in opts:
            opts.discard("keep_sender")
        else:
            opts.add("keep_sender")
            opts.discard("hide_header")
            opts.discard("text_only")
            opts.discard("media_only")
    elif key == "hide_header":
        if "hide_header" in opts:
            opts.discard("hide_header")
        else:
            opts.add("hide_header")
            opts.discard("keep_sender")
            opts.discard("text_only")
            opts.discard("media_only")
    elif key == "remove_captions":
        opts.symmetric_difference_update({"remove_captions"})
    elif key == "text_only":
        if "text_only" in opts:
            opts.discard("text_only")
        else:
            opts.add("text_only")
            opts.discard("media_only")
            if wiz.mode in ("forward", "copy"):
                wiz.mode = "copy"
                opts.discard("keep_sender")
    elif key == "media_only":
        if "media_only" in opts:
            opts.discard("media_only")
        else:
            opts.add("media_only")
            opts.discard("text_only")
            if wiz.mode in ("forward", "copy"):
                wiz.mode = "copy"
                opts.discard("keep_sender")
    await edit(event, text.options_prompt(wiz.mode, opts), keyboards.options_keyboard(wiz.mode, opts))
    return True


async def _ask_filter(bot, event, uid: int) -> bool:
    wiz = store.get_transfer(uid)
    await edit(event, text.filter_prompt(), keyboards.filter_keyboard(wiz.filter_type))
    return True


async def _on_filter(bot, event, uid: int, key: str) -> bool:
    wiz = store.get_transfer(uid)
    wiz.filter_type = key
    await edit(event, text.filter_prompt(), keyboards.filter_keyboard(wiz.filter_type))
    return True


async def _ask_dedup(bot, event, uid: int) -> bool:
    wiz = store.get_transfer(uid)
    await edit(event, text.dedup_prompt(wiz.dedup), keyboards.dedup_keyboard(wiz.dedup))
    return True


async def _ask_schedule(bot, event, uid: int) -> bool:
    await edit(event, text.schedule_prompt(), keyboards.schedule_keyboard())
    return True


async def _on_schedule(bot, event, uid: int, kind: str) -> bool:
    wiz = store.get_transfer(uid)
    if kind == "now":
        wiz.schedule_kind = "now"
        wiz.schedule_time = None
        wiz.schedule_weekday = None
        return await _show_summary(bot, event, uid)
    if kind == "weekly":
        wiz.schedule_kind = "weekly"
        await edit(event, text.schedule_weekday_prompt(), keyboards.weekday_keyboard())
        return True
    if kind in ("daily", "later"):
        wiz.schedule_kind = kind
        store.set_pending(uid, "tr_sched_time")
        await edit(event, text.schedule_time_prompt(), keyboards.back_row())
        return True
    return True


async def _show_summary(bot, event, uid: int) -> bool:
    wiz = store.get_transfer(uid)
    cfg = {
        "mode": wiz.mode,
        "options": set(wiz.options),
        "filter_type": wiz.filter_type,
        "filter_label": filter_label(wiz.filter_type),
        "dedup": wiz.dedup,
        "schedule_kind": wiz.schedule_kind,
        "schedule_time": wiz.schedule_time,
        "schedule_weekday": wiz.schedule_weekday,
    }
    if wiz.count_mode == "latest":
        cfg["count_label"] = f"Latest {wiz.count} (filtered)"
    else:
        cfg["count_label"] = f"IDs {wiz.custom_start} → {wiz.custom_end}"
    await edit(event, text.summary(cfg, wiz.source, wiz.dest), keyboards.summary_keyboard())
    return True


async def _edit_step(bot, event, uid: int, step: str) -> bool:
    if step == "src":
        return await _ask_source(bot, event, uid)
    if step == "dst":
        return await _ask_dest(bot, event, uid)
    if step == "count":
        return await _ask_count(bot, event, uid)
    if step == "mode":
        return await _ask_mode(bot, event, uid)
    if step == "opts":
        wiz = store.get_transfer(uid)
        await edit(event, text.options_prompt(wiz.mode, wiz.options), keyboards.options_keyboard(wiz.mode, wiz.options))
        return True
    if step == "filter":
        return await _ask_filter(bot, event, uid)
    if step == "dedup":
        return await _ask_dedup(bot, event, uid)
    if step == "sched":
        return await _ask_schedule(bot, event, uid)
    return True


# ----------------------------------------------------------------------
# run / stop / save
# ----------------------------------------------------------------------
async def _build_cfg(uid: int, wiz: TransferWizard) -> TransferConfig | None:
    sid = await db.get_active_sid(uid)
    if not sid:
        return None
    client = await client_pool.get(uid, sid)
    src_entity = await client.get_entity(wiz.source["id"])
    dst_entity = await client.get_entity(wiz.dest["id"])
    settings = await db.get_settings(uid)
    if wiz.count_mode == "latest":
        ids = await engine.collect_ids(client, src_entity, wiz.count, None, None, wiz.filter_type)
    else:
        ids = await engine.collect_ids(client, src_entity, None, wiz.custom_start, wiz.custom_end, wiz.filter_type)
    cfg = TransferConfig(
        source_entity=src_entity,
        dest_entity=dst_entity,
        message_ids=ids,
        mode=wiz.mode,
        options=set(wiz.options),
        dedup=wiz.dedup,
        threads=settings["threads"],
        forward_delay=settings["forward_delay"],
        retry_count=settings["retry_count"],
        handle_flood=settings["handle_flood"],
        auto_resume=settings["auto_resume"],
        sid=sid,
        source_name=wiz.source["name"],
        dest_name=wiz.dest["name"],
    )
    cfg.total_planned = len(ids)
    return cfg


async def _run(bot, event, uid: int) -> bool:
    if uid in store.running:
        await answer(event, "A transfer is already running. Stop it first.", alert=True)
        return True
    wiz = store.get_transfer(uid)
    try:
        cfg = await _build_cfg(uid, wiz)
    except Exception as exc:
        log.warning("build cfg failed: %s", exc)
        await edit(event, f"⚠️ Could not prepare the transfer:\n<code>{str(exc)[:300]}</code>", keyboards.back_row())
        return True
    if cfg is None or not cfg.message_ids:
        await edit(event, "⚠️ No messages matched your filters / range.", keyboards.back_row())
        return True
    await execute(bot, uid, cfg)
    return True


async def execute(bot, uid: int, cfg: TransferConfig) -> TransferResult:
    """Shared runner used by the wizard and saved jobs."""
    settings = await db.get_settings(uid)
    progress_msg = await bot.send_message(
        uid,
        text.progress_text(0, cfg.total_planned, 0, 0, 0, 0, cfg.mode, settings["dark_theme"]),
        buttons=keyboards.running_keyboard(),
        parse_mode="html",
    )
    engine_obj = TransferEngine()
    store.running[uid] = engine_obj
    lock = asyncio.Lock()
    last_edit = {"t": 0.0}

    log_id = await db.add_log(
        {
            "user_id": uid,
            "sid": cfg.sid,
            "source_id": cfg.source_entity.id,
            "source_name": cfg.source_name,
            "dest_id": cfg.dest_entity.id,
            "dest_name": cfg.dest_name,
            "mode": cfg.mode,
            "total": cfg.total_planned,
            "status": "running",
        }
    )

    async def progress_cb(state: dict) -> None:
        elapsed = state["elapsed"]
        if elapsed - last_edit["t"] < 0.8 and state["success"] + state["skipped"] + state["failed"] < state["total"]:
            return
        last_edit["t"] = elapsed
        async with lock:
            try:
                await bot.edit_message(
                    uid,
                    progress_msg.id,
                    text.progress_text(
                        state["success"] + state["skipped"] + state["failed"],
                        state["total"],
                        state["elapsed"],
                        state["speed"],
                        state["skipped"],
                        state["failed"],
                        cfg.mode,
                        settings["dark_theme"],
                    ),
                    buttons=keyboards.running_keyboard(),
                    parse_mode="html",
                )
            except (MessageNotModifiedError, MessageIdInvalidError):
                pass
            except Exception:
                log.debug("progress edit failed", exc_info=True)

    try:
        result = await engine_obj.run(client=await client_pool.get(uid, cfg.sid), cfg=cfg, progress_cb=progress_cb)
    finally:
        store.running.pop(uid, None)

    if result.cancelled or result.error:
        final_text = text.run_failed(
            {"total": result.total, "success": result.success, "skipped": result.skipped, "failed": result.failed},
            result.duration,
            result.error,
        )
    else:
        final_text = text.run_done(
            {"total": result.total, "success": result.success, "skipped": result.skipped, "failed": result.failed},
            result.duration,
        )
    try:
        await bot.edit_message(uid, progress_msg.id, final_text, buttons=keyboards.run_done_keyboard(), parse_mode="html")
    except Exception:
        pass

    await db.update_log(
        log_id,
        {
            "status": "done",
            "ended_at": now(),
            "success": result.success,
            "skipped": result.skipped,
            "failed": result.failed,
            "duration": result.duration,
            "cancelled": result.cancelled,
        },
    )
    return result


async def _stop(bot, event, uid: int) -> bool:
    engine_obj = store.running.get(uid)
    if engine_obj is not None:
        engine_obj.request_stop()
        await answer(event, "Stopping after the current message...")
    else:
        await answer(event, "Nothing is running")
    return True


async def _ask_job_name(bot, event, uid: int) -> bool:
    store.set_pending(uid, "tr_job_name")
    await edit(event, "💾 Give this transfer a <b>name</b> so you can run it again:", keyboards.back_row())
    return True


async def save_job(uid: int, wiz: TransferWizard, name: str) -> str:
    settings = await db.get_settings(uid)
    job = {
        "user_id": uid,
        "sid": await db.get_active_sid(uid),
        "name": name,
        "source": {"id": wiz.source["id"], "name": wiz.source["name"]},
        "dest": {"id": wiz.dest["id"], "name": wiz.dest["name"]},
        "count_mode": wiz.count_mode,
        "count": wiz.count,
        "custom_start": wiz.custom_start,
        "custom_end": wiz.custom_end,
        "mode": wiz.mode,
        "options": sorted(wiz.options),
        "filter_type": wiz.filter_type,
        "dedup": wiz.dedup,
        "threads": settings["threads"],
        "forward_delay": settings["forward_delay"],
        "retry_count": settings["retry_count"],
        "handle_flood": settings["handle_flood"],
        "auto_resume": settings["auto_resume"],
        "schedule_kind": wiz.schedule_kind,
        "schedule_time": wiz.schedule_time,
        "schedule_weekday": wiz.schedule_weekday,
    }
    if wiz.schedule_kind == "later" and wiz.schedule_time:
        job["status"] = "scheduled"
        job["next_run"] = schedule_instant("later", wiz.schedule_time)
    elif wiz.schedule_kind == "daily" and wiz.schedule_time:
        job["status"] = "scheduled"
        job["next_run"] = _compute_next("daily", wiz.schedule_time, None)
    elif wiz.schedule_kind == "weekly" and wiz.schedule_time:
        job["status"] = "scheduled"
        job["next_run"] = _compute_next("weekly", wiz.schedule_time, wiz.schedule_weekday)
    else:
        job["status"] = "saved"
    return await db.save_job(job)


# ----------------------------------------------------------------------
# pending text input
# ----------------------------------------------------------------------
async def handle_pending(bot, event: events.NewMessage.Event, kind: str) -> bool:
    uid = event.sender_id
    if kind == "tr_source":
        return await _on_source_input(bot, event, uid)
    if kind == "tr_dest_manual":
        return await _on_dest_input(bot, event, uid)
    if kind == "tr_start_id":
        return await _on_start_id(bot, event, uid)
    if kind == "tr_end_id":
        return await _on_end_id(bot, event, uid)
    if kind == "tr_sched_time":
        return await _on_sched_time(bot, event, uid)
    if kind == "tr_job_name":
        return await _on_job_name(bot, event, uid)
    if kind == "tr_link_start":
        return await _on_link_start_input(bot, event, uid)
    if kind == "tr_link_end":
        return await _on_link_end_input(bot, event, uid)
    return False


async def _on_source_input(bot, event, uid: int) -> bool:
    wiz = store.get_transfer(uid)
    sid = await db.get_active_sid(uid)
    if not sid:
        await event.respond(text.err_no_account())
        return True
    try:
        client = await client_pool.get(uid, sid)
        if event.message.forward:
            resolved = await resolve_forwarded(client, event.message)
        else:
            resolved = await resolve(client, event.raw_text)
    except ValueError as exc:
        log.warning("source resolve error: %s", exc)
        await event.respond("⚠️ Session issue: please go to 👤 Accounts → 🗑 Delete Account, then ➕ Add Account again.")
        return True
    except Exception as exc:
        log.warning("source resolve error: %s", exc)
        await event.respond(f"⚠️ Resolution failed: <code>{str(exc)[:200]}</code>")
        return True
    if resolved is None:
        await event.respond("⚠️ Could not resolve that source. Send a valid link, username, ID, or a forwarded message.")
        return True
    wiz.source = {"id": resolved.chat_id, "name": resolved.title}
    store.set_pending(uid, None)
    await event.respond(text.source_confirmed(resolved.title, resolved.chat_id), buttons=keyboards.source_confirm_keyboard(), parse_mode="html")
    return True


async def _on_dest_input(bot, event, uid: int) -> bool:
    wiz = store.get_transfer(uid)
    sid = await db.get_active_sid(uid)
    try:
        client = await client_pool.get(uid, sid)
        resolved = await resolve(client, event.raw_text)
    except Exception as exc:
        log.warning("dest resolve error: %s", exc)
        await event.respond(f"⚠️ Resolution failed: <code>{str(exc)[:200]}</code>")
        return True
    if resolved is None:
        await event.respond("⚠️ Could not resolve that destination. Send a valid link, username or ID.")
        return True
    wiz.dest = {"id": resolved.chat_id, "name": resolved.title}
    store.set_pending(uid, None)
    await event.respond(text.dest_confirmed(resolved.title, resolved.chat_id), buttons=keyboards.dest_confirm_keyboard(), parse_mode="html")
    return True


async def _on_start_id(bot, event, uid: int) -> bool:
    wiz = store.get_transfer(uid)
    value = event.raw_text.strip()
    if not value.isdigit():
        await event.respond("⚠️ Please send a number.")
        return True
    wiz.custom_start = int(value)
    store.set_pending(uid, "tr_end_id")
    await event.respond(text.custom_end_prompt())
    return True


async def _on_end_id(bot, event, uid: int) -> bool:
    wiz = store.get_transfer(uid)
    value = event.raw_text.strip()
    if not value.isdigit():
        await event.respond("⚠️ Please send a number.")
        return True
    wiz.custom_end = int(value)
    wiz.count_mode = "custom"
    store.set_pending(uid, None)
    await _ask_mode(bot, event, uid)
    return True


async def _on_link_start_input(bot, event, uid: int) -> bool:
    wiz = store.get_transfer(uid)
    sid = await db.get_active_sid(uid)
    if not sid:
        await event.respond(text.err_no_account())
        return True
    try:
        client = await client_pool.get(uid, sid)
        parsed = parse_input(event.raw_text)
        if parsed["kind"] == "unknown":
            await event.respond("⚠️ Send a valid message link, e.g. https://t.me/channel/123, https://t.me/joinchat/abc123/123, or https://t.me/c/123456789/123")
            return True
        if parsed["kind"] == "message_link":
            if parsed.get("slug") == "c":
                entity = await _resolve_private_channel(client, parsed["cid"])
                if entity is None:
                    await event.respond("⚠️ Could not access this private channel. Make sure your account is a member.")
                    return True
            else:
                identifier = parsed.get("identifier") or parsed.get("slug")
                entity = await client.get_entity(identifier)
            msg_id = parsed.get("msg_id")
        elif parsed["kind"] == "username":
            entity = await client.get_entity(parsed["username"])
            msg_id = None
        elif parsed["kind"] == "channel_id":
            entity = await client.get_entity(parsed["id"])
            msg_id = None
        else:
            await event.respond("⚠️ Send a valid message link, e.g. https://t.me/channel/123, https://t.me/joinchat/abc123/123, or https://t.me/c/123456789/123")
            return True
        if msg_id is None:
            await event.respond("⚠️ Please send a message link that includes the message ID, e.g. https://t.me/channel/123")
            return True
        wiz.source = {"id": entity.id, "name": getattr(entity, "title", getattr(entity, "username", str(entity.id)))}
        wiz.custom_start = msg_id
        store.set_pending(uid, "tr_link_end")
        await event.respond(
            f"✅ <b>Start message set</b> — <code>{msg_id}</code>\n\n"
            f"Now send the <b>end message link</b> from the same chat.",
            parse_mode="html",
        )
        return True
    except Exception as exc:
        log.warning("link resolve error: %s", exc)
        await event.respond(
            f"⚠️ Could not resolve that link: <code>{str(exc)[:200]}</code>\n\n"
            f"Make sure the link format is correct and your account has access to the chat.",
            parse_mode="html",
        )
        return True


async def _on_link_end_input(bot, event, uid: int) -> bool:
    wiz = store.get_transfer(uid)
    sid = await db.get_active_sid(uid)
    if not sid:
        await event.respond(text.err_no_account())
        return True
    try:
        client = await client_pool.get(uid, sid)
        parsed = parse_input(event.raw_text)
        if parsed["kind"] == "unknown":
            await event.respond("⚠️ Send a valid message link, e.g. https://t.me/channel/500, https://t.me/joinchat/abc123/500, or https://t.me/c/123456789/500")
            return True
        if parsed["kind"] == "message_link":
            if parsed.get("slug") == "c":
                end_entity = await _resolve_private_channel(client, parsed["cid"])
                if end_entity is None:
                    await event.respond("⚠️ Could not access this private channel. Make sure your account is a member.")
                    return True
                end_chat_id = end_entity.id
            else:
                end_identifier = parsed.get("identifier") or parsed.get("slug")
                end_entity = await client.get_entity(end_identifier)
                end_chat_id = end_entity.id
            end_msg_id = parsed.get("msg_id")
        else:
            await event.respond("⚠️ Send a message link with a message ID, not just a username or ID.")
            return True
        if wiz.source["id"] != end_chat_id:
            await event.respond("⚠️ The end link must be from the same source chat as the start link.")
            return True
        if end_msg_id < wiz.custom_start:
            await event.respond("⚠️ End message ID must be greater than start message ID.")
            return True
        wiz.custom_end = end_msg_id
        wiz.count_mode = "custom"
        store.set_pending(uid, None)
        await event.respond(
            f"✅ <b>Range set</b>\n\n"
            f"From message <code>{wiz.custom_start}</code> to <code>{end_msg_id}</code>\n"
            f"Total: <b>{end_msg_id - wiz.custom_start + 1}</b> messages",
            parse_mode="html",
        )
        return await _ask_mode(bot, event, uid)
    except Exception as exc:
        log.warning("end link resolve error: %s", exc)
        await event.respond(
            f"⚠️ Could not resolve that link: <code>{str(exc)[:200]}</code>\n\n"
            f"Make sure the link format is correct and your account has access to the chat.",
            parse_mode="html",
        )
        return True


async def _on_sched_time(bot, event, uid: int) -> bool:
    wiz = store.get_transfer(uid)
    value = event.raw_text.strip()
    try:
        hh, mm = (int(x) for x in value.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except ValueError:
        await event.respond("⚠️ Use <b>HH:MM</b> 24h format, e.g. <code>21:30</code>.")
        return True
    wiz.schedule_time = f"{hh:02d}:{mm:02d}"
    store.set_pending(uid, None)
    await _show_summary_from_message(bot, event, uid)
    return True


async def _on_job_name(bot, event, uid: int) -> bool:
    wiz = store.get_transfer(uid)
    name = event.raw_text.strip()[:50] or "Transfer"
    store.set_pending(uid, None)
    try:
        jid = await save_job(uid, wiz, name)
    except Exception as exc:
        log.warning("save job failed: %s", exc)
        await event.respond(f"⚠️ Could not save the job: <code>{str(exc)[:200]}</code>")
        return True
    await event.respond(text.job_saved(name, jid), buttons=keyboards.run_done_keyboard(), parse_mode="html")
    return True


async def _show_summary_from_message(bot, event, uid: int) -> None:
    wiz = store.get_transfer(uid)
    cfg = {
        "mode": wiz.mode,
        "options": set(wiz.options),
        "filter_type": wiz.filter_type,
        "filter_label": filter_label(wiz.filter_type),
        "dedup": wiz.dedup,
        "schedule_kind": wiz.schedule_kind,
        "schedule_time": wiz.schedule_time,
        "schedule_weekday": wiz.schedule_weekday,
    }
    if wiz.count_mode == "latest":
        cfg["count_label"] = f"Latest {wiz.count} (filtered)"
    else:
        cfg["count_label"] = f"IDs {wiz.custom_start} → {wiz.custom_end}"
    await event.respond(text.summary(cfg, wiz.source, wiz.dest), buttons=keyboards.summary_keyboard(), parse_mode="html")
