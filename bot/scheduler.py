"""Scheduler: runs due "later / daily / weekly" jobs in the background."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from bot import text
from bot.client_pool import client_pool
from bot.config import config
from bot.db import db
from bot.transfer_engine import TransferConfig, TransferEngine

log = logging.getLogger("bot.scheduler")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _compute_next(kind: str, time_str: str | None, weekday: int | None, base: datetime | None = None) -> datetime | None:
    """Next run instant for daily/weekly schedules."""
    if kind not in ("daily", "weekly") or not time_str:
        return None
    try:
        hh, mm = (int(x) for x in time_str.split(":"))
    except ValueError:
        return None
    base = base or utcnow()
    nxt = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if nxt <= base:
        nxt += timedelta(days=1)
    if kind == "weekly" and weekday is not None:
        while nxt.weekday() != weekday:
            nxt += timedelta(days=1)
    return nxt


def schedule_instant(kind: str, time_str: str | None) -> datetime | None:
    """For 'later', a one-off run scheduled at the next occurrence of time."""
    if kind != "later" or not time_str:
        return None
    return _compute_next("daily", time_str, weekday=None)


class Scheduler:
    def __init__(self, bot_client, engine: TransferEngine | None = None) -> None:
        self.bot = bot_client
        self.engine = engine or TransferEngine()
        self._executing: set[str] = set()
        self._sem = asyncio.Semaphore(2)

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(config.SCHEDULER_INTERVAL)
            try:
                await self._check_mongo()
            except Exception:
                log.exception("mongo health check failed")
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")

    async def _check_mongo(self) -> None:
        if db.client is None:
            return
        if not await db.ping():
            log.warning("MongoDB ping failed, attempting reconnect")
            ok = await db.reconnect()
            if ok:
                log.info("MongoDB reconnected")
            else:
                log.warning("MongoDB reconnect failed, will retry next interval")

    async def tick(self) -> None:
        due = await db.due_jobs(utcnow())
        for job in due:
            jid = str(job["_id"])
            if jid in self._executing:
                continue
            self._executing.add(jid)
            asyncio.create_task(self._run_guarded(job))

    async def _run_guarded(self, job: dict) -> None:
        jid = str(job["_id"])
        try:
            async with self._sem:
                await self.execute_job(job)
        except Exception:
            log.exception("scheduled job %s failed", jid)
        finally:
            self._executing.discard(jid)

    # ------------------------------------------------------------------
    async def execute_job(self, job: dict) -> None:
        jid = str(job["_id"])
        user_id = job["user_id"]
        sid = job["sid"]
        try:
            await db.mark_running(jid)
            # Use the shared client pool: exactly one client per account, so
            # scheduled jobs never spin up a second session for the same
            # account (which is what causes "wrong session ID" warnings).
            client = await client_pool.get(user_id, sid)
            # A fresh engine per run: concurrent jobs must never share the
            # mutable run state (stop/skip/pause/progress) of one instance.
            engine = TransferEngine()
            cfg = await self._build_cfg(job, client, engine)
            log_doc = {
                "user_id": user_id,
                "jid": jid,
                "job_name": job.get("name", ""),
                "sid": sid,
                "source_id": cfg.source_entity.id,
                "source_name": cfg.source_name,
                "dest_id": cfg.dest_entity.id,
                "dest_name": cfg.dest_name,
                "mode": cfg.mode,
                "total": cfg.total_planned,
                "status": "running",
            }
            log_id = await db.add_log(log_doc)

            async def refresh_client():
                # Rebuild the account client after repeated network errors so a
                # scheduled run self-heals instead of retrying forever against a
                # half-dead connection.
                try:
                    return await client_pool.refresh(user_id, sid)
                except Exception as exc:
                    log.warning("client refresh failed for user %s: %s", user_id, exc)
                    return None

            async with client_pool.use(user_id, sid):
                result = await engine.run(client, cfg, refresh_client=refresh_client)
            await db.update_log(
                log_id,
                {
                    "status": "done",
                    "ended_at": utcnow(),
                    "success": result.success,
                    "skipped": result.skipped,
                    "failed": result.failed,
                    "duration": result.duration,
                },
            )
            await self._advance_schedule(job, result)
            await self._notify(user_id, job, result)
        except Exception as exc:  # noqa: BLE001
            log.exception("scheduled job %s errored", jid)
            await db.update_job(jid, {"status": "error", "last_error": str(exc)[:500]})
            await self._notify_error(user_id, job, exc)

    async def _build_cfg(self, job: dict, client, engine: TransferEngine) -> TransferConfig:
        src = job["source"]
        dst = job["dest"]
        source_entity = await asyncio.wait_for(
            client.get_entity(src["id"]), timeout=config.OP_TIMEOUT
        )
        dest_entity = await asyncio.wait_for(
            client.get_entity(dst["id"]), timeout=config.OP_TIMEOUT
        )
        count_mode = job.get("count_mode", "latest")
        if count_mode == "latest":
            ids = await engine.collect_ids(
                client, source_entity, job.get("count", 10),
                None, None, job.get("filter_type", "all"),
            )
        else:
            ids = await engine.collect_ids(
                client, source_entity, None,
                job.get("custom_start"), job.get("custom_end"), job.get("filter_type", "all"),
            )
        cfg = TransferConfig(
            source_entity=source_entity,
            dest_entity=dest_entity,
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
            source_name=src.get("name", ""),
            dest_name=dst.get("name", ""),
        )
        cfg.total_planned = len(ids)
        return cfg

    async def _advance_schedule(self, job: dict, result) -> None:
        kind = job.get("schedule_kind")
        jid = str(job["_id"])
        if kind == "later":
            await db.update_job(jid, {"status": "done", "last_run": utcnow()})
        elif kind in ("daily", "weekly"):
            nxt = _compute_next(kind, job.get("schedule_time"), job.get("schedule_weekday"))
            await db.update_job(
                jid,
                {
                    "status": "scheduled",
                    "next_run": nxt,
                    "last_run": utcnow(),
                    "last_summary": {
                        "success": result.success,
                        "skipped": result.skipped,
                        "failed": result.failed,
                    },
                },
            )
        else:
            await db.update_job(jid, {"status": "done", "last_run": utcnow()})

    async def _notify(self, user_id: int, job: dict, result) -> None:
        settings = await db.get_settings(user_id)
        if not settings.get("notifications"):
            return
        try:
            await self.bot.send_message(
                user_id,
                text.run_done(
                    {
                        "total": result.total,
                        "success": result.success,
                        "skipped": result.skipped,
                        "failed": result.failed,
                    },
                    result.duration,
                    result.success / result.duration if result.duration > 0 else 0.0,
                    job.get("dest", {}).get("name", ""),
                )
                + f"\n\nJob: <b>{job.get('name', '')}</b>",
            )
        except Exception:
            log.exception("failed to notify user %s", user_id)

    async def _notify_error(self, user_id: int, job: dict, exc: Exception) -> None:
        settings = await db.get_settings(user_id)
        if not settings.get("notifications"):
            return
        try:
            await self.bot.send_message(
                user_id,
                f"🛑 Scheduled job <b>{job.get('name', '')}</b> failed:\n"
                f"<code>{str(exc)[:500]}</code>",
            )
        except Exception:
            log.exception("failed to notify error to user %s", user_id)
