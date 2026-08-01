"""Saved jobs: list, run again, delete."""
from __future__ import annotations

import logging

from telethon import Button, events

from bot import keyboards, text
from bot.client_pool import client_pool
from bot.config import config
from bot.db import db
from bot.handlers import transfer as transfer_handler
from bot.handlers.common import answer, edit
from bot.transfer_engine import TransferConfig, TransferEngine

log = logging.getLogger("bot.jobs")

engine = TransferEngine()


async def handle(bot, event: events.CallbackQuery.Event, data: str) -> bool:
    if data == "jobs" or data.startswith("jobs:"):
        return await _route(bot, event, data)
    return False


async def _route(bot, event, data: str) -> bool:
    uid = event.sender_id
    if data == "jobs":
        return await _show(bot, event, uid)
    if data.startswith("jobs:run:"):
        return await _run(bot, event, uid, data.split(":", 2)[2])
    if data.startswith("jobs:del:"):
        return await _confirm_delete(bot, event, uid, data.split(":", 2)[2])
    if data.startswith("jobs:del2:"):
        return await _delete(bot, event, uid, data.split(":", 2)[2])
    return False


async def _show(bot, event, uid: int) -> bool:
    jobs = await db.user_jobs(uid)
    await edit(event, text.jobs_menu(jobs), keyboards.jobs_menu_keyboard(jobs))
    return True


async def _run(bot, event, uid: int, jid: str) -> bool:
    if uid in transfer_handler.store.running:
        await answer(event, "A transfer is already running. Stop it first.", alert=True)
        return True
    job = await db.get_job(jid)
    if not job or job["user_id"] != uid:
        await answer(event, "Job not found", alert=True)
        return True
    sid = job.get("sid")
    if not sid:
        await answer(event, "Job has no account attached", alert=True)
        return True
    try:
        client = await client_pool.get(uid, sid)
        cfg = await _cfg_from_job(client, job)
    except Exception as exc:
        log.warning("job run prep failed: %s", exc)
        await edit(event, f"⚠️ Could not prepare job:\n<code>{str(exc)[:300]}</code>", keyboards.back_row())
        return True
    if not cfg.message_ids:
        await edit(event, "⚠️ No messages matched this job's filters/range.", keyboards.back_row())
        return True
    await transfer_handler.execute(bot, uid, cfg)
    return True


async def _confirm_delete(bot, event, uid: int, jid: str) -> bool:
    job = await db.get_job(jid)
    if not job or job["user_id"] != uid:
        await answer(event, "Job not found", alert=True)
        return True
    name = job.get("name", "Unnamed")
    kb = [
        [Button.inline("🗑 Yes, delete", f"jobs:del2:{jid}".encode())],
        [Button.inline("🔙 Cancel", b"jobs")],
    ]
    await edit(event, f"Delete job <b>{name}</b>?", kb)
    return True


async def _delete(bot, event, uid: int, jid: str) -> bool:
    await db.delete_job(uid, jid)
    await answer(event, "Job deleted")
    await _show(bot, event, uid)
    return True


async def _cfg_from_job(client, job: dict) -> TransferConfig:
    src = await client.get_entity(job["source"]["id"])
    dst = await client.get_entity(job["dest"]["id"])
    if job.get("count_mode") == "latest":
        ids = await engine.collect_ids(
            client, src, job.get("count", 10), None, None, job.get("filter_type", "all")
        )
    else:
        ids = await engine.collect_ids(
            client, src, None, job.get("custom_start"), job.get("custom_end"), job.get("filter_type", "all")
        )
    cfg = TransferConfig(
        source_entity=src,
        dest_entity=dst,
        message_ids=ids,
        mode=job.get("mode", "forward"),
        options=set(job.get("options", [])),
        dedup=job.get("dedup", True),
        threads=job.get("threads", config.DEFAULT_THREADS),
        forward_delay=job.get("forward_delay", 0.0),
        retry_count=job.get("retry_count", 3),
        handle_flood=job.get("handle_flood", True),
        auto_resume=job.get("auto_resume", True),
        sid=job.get("sid", ""),
        source_name=job.get("source", {}).get("name", ""),
        dest_name=job.get("dest", {}).get("name", ""),
    )
    cfg.total_planned = len(ids)
    return cfg
