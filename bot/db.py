"""MongoDB data layer (async via motor).

Collections:
    users                - bot users + per-user settings + active account
    sessions             - encrypted Telethon sessions per account
    jobs                 - saved / scheduled transfer jobs
    logs                 - transfer run logs
    settings             - per-user settings (preference doc)
    transferred_messages - dedup store of transferred message ids
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from bot.config import config


class InMemoryCollection:
    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []

    @staticmethod
    def _matches(doc: dict[str, Any], filter: dict[str, Any]) -> bool:
        from bson import ObjectId

        for k, v in filter.items():
            if k == "$in":
                values = v
                if doc.get("_id") in values or str(doc.get("_id")) in [str(x) for x in values]:
                    continue
                return False
            stored = doc.get(k)
            if isinstance(v, ObjectId) or isinstance(stored, ObjectId):
                if str(stored) != str(v):
                    return False
            elif stored != v:
                return False
        return True

    async def create_index(self, *args, **kwargs) -> None:
        return None

    async def insert_one(self, doc: dict[str, Any]) -> Any:
        # mimic a real MongoDB ObjectId so str(inserted_id) is ObjectId-parseable
        inserted_id = secrets.token_hex(12)
        doc.setdefault("_id", inserted_id)
        self._docs.append(doc)
        return type("Result", (), {"inserted_id": inserted_id})()

    async def update_one(self, filter: dict[str, Any], update: dict[str, Any], upsert: bool = False) -> None:
        for doc in self._docs:
            if self._matches(doc, filter):
                if "$set" in update:
                    doc.update(update["$set"])
                if "$setOnInsert" in update:
                    for key, value in update["$setOnInsert"].items():
                        doc.setdefault(key, value)
                return None

        if not upsert:
            return None

        new_doc = dict(filter)
        if "$setOnInsert" in update:
            new_doc.update(update["$setOnInsert"])
        if "$set" in update:
            new_doc.update(update["$set"])
        await self.insert_one(new_doc)
        return None

    async def find_one(self, filter: dict[str, Any]) -> dict[str, Any] | None:
        for doc in self._docs:
            if self._matches(doc, filter):
                return doc
        return None

    async def delete_one(self, filter: dict[str, Any]) -> None:
        self._docs = [doc for doc in self._docs if not self._matches(doc, filter)]

    async def delete_many(self, filter: dict[str, Any]) -> Any:
        deleted = [doc for doc in self._docs if self._matches(doc, filter)]
        self._docs = [doc for doc in self._docs if not self._matches(doc, filter)]
        return type("Result", (), {"deleted_count": len(deleted)})()

    async def count_documents(self, filter: dict[str, Any]) -> int:
        return sum(1 for doc in self._docs if self._matches(doc, filter))

    def find(self, filter: dict[str, Any]):
        class Cursor:
            def __init__(self, docs):
                self._docs = docs

            def sort(self, *args, **kwargs):
                return self

            def limit(self, *args, **kwargs):
                return self

            def __aiter__(self):
                self._iter = iter(self._docs)
                return self

            async def __anext__(self):
                try:
                    return next(self._iter)
                except StopIteration:
                    raise StopAsyncIteration

        return Cursor([doc for doc in self._docs if self._matches(doc, filter)])

    async def aggregate(self, pipeline: list[dict[str, Any]]):
        return []


class InMemoryDatabase:
    def __init__(self) -> None:
        self.users = InMemoryCollection()
        self.sessions = InMemoryCollection()
        self.jobs = InMemoryCollection()
        self.logs = InMemoryCollection()
        self.settings = InMemoryCollection()
        self.transferred = InMemoryCollection()


log = logging.getLogger("bot.db")


def now() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    def __init__(self, uri: str = "", db_name: str = "") -> None:
        self._uri = uri or config.MONGO_URI
        self._db_name = db_name or config.MONGO_DB
        self.client: AsyncIOMotorClient | None = None
        self.db = None  # type: ignore[assignment]
        self.users = None  # type: ignore[assignment]
        self.sessions = None  # type: ignore[assignment]
        self.jobs = None  # type: ignore[assignment]
        self.logs = None  # type: ignore[assignment]
        self.settings = None  # type: ignore[assignment]
        self.transferred = None  # type: ignore[assignment]

    async def init(self) -> None:
        try:
            self.client = AsyncIOMotorClient(self._uri, serverSelectionTimeoutMS=5000)
            self.db = self.client[self._db_name]
            self.users = self.db["users"]
            self.sessions = self.db["sessions"]
            self.jobs = self.db["jobs"]
            self.logs = self.db["logs"]
            self.settings = self.db["settings"]
            self.transferred = self.db["transferred_messages"]
            await self._ensure_indexes()
        except Exception as exc:  # pragma: no cover - defensive fallback
            log.warning("MongoDB unavailable, falling back to in-memory store: %s", exc)
            in_memory = InMemoryDatabase()
            self.client = None
            self.db = None
            self.users = in_memory.users
            self.sessions = in_memory.sessions
            self.jobs = in_memory.jobs
            self.logs = in_memory.logs
            self.settings = in_memory.settings
            self.transferred = in_memory.transferred

    async def _ensure_indexes(self) -> None:
        await self.users.create_index("user_id", unique=True)
        await self.sessions.create_index("sid", unique=True)
        await self.sessions.create_index([("user_id", 1), ("created_at", -1)])
        await self.jobs.create_index([("user_id", 1), ("created_at", -1)])
        await self.jobs.create_index([("next_run", 1), ("schedule_kind", 1)])
        await self.logs.create_index([("user_id", 1), ("started_at", -1)])
        await self.transferred.create_index(
            [("source_id", 1), ("dest_id", 1), ("msg_id", 1)], unique=True
        )
        await self.transferred.create_index([("sid", 1), ("transferred_at", -1)])

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    async def upsert_user(self, user_id: int, first_name: str = "", username: str = "") -> None:
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$setOnInsert": {
                    "user_id": user_id,
                    "first_name": first_name,
                    "username": username,
                    "created_at": now(),
                },
                "$set": {"last_seen": now()},
            },
            upsert=True,
        )

    async def get_user(self, user_id: int) -> dict | None:
        return await self.users.find_one({"user_id": user_id})

    async def set_active_sid(self, user_id: int, sid: str | None) -> None:
        await self.users.update_one(
            {"user_id": user_id}, {"$set": {"active_sid": sid}}, upsert=True
        )

    async def get_active_sid(self, user_id: int) -> str | None:
        user = await self.get_user(user_id)
        return user.get("active_sid") if user else None

    # ------------------------------------------------------------------
    # sessions (accounts)
    # ------------------------------------------------------------------
    async def add_session(
        self,
        user_id: int,
        phone: str,
        name: str,
        encrypted: str,
        tg_user_id: int,
    ) -> dict:
        sid = secrets.token_hex(4)
        doc = {
            "sid": sid,
            "user_id": user_id,
            "phone": phone,
            "name": name,
            "encrypted_session": encrypted,
            "tg_user_id": tg_user_id,
            "created_at": now(),
            "last_used": now(),
        }
        await self.sessions.insert_one(doc)
        return doc

    async def get_user_sessions(self, user_id: int) -> list[dict]:
        cur = self.sessions.find({"user_id": user_id}).sort("created_at", -1)
        return [d async for d in cur]

    async def get_session(self, user_id: int, sid: str) -> dict | None:
        return await self.sessions.find_one({"user_id": user_id, "sid": sid})

    async def delete_session(self, user_id: int, sid: str) -> None:
        await self.sessions.delete_one({"user_id": user_id, "sid": sid})
        await self.transferred.delete_many({"sid": sid})
        if await self.get_active_sid(user_id) == sid:
            await self.set_active_sid(user_id, None)

    async def touch_session(self, sid: str) -> None:
        await self.sessions.update_one({"sid": sid}, {"$set": {"last_used": now()}})

    async def get_all_sessions(self) -> list[dict]:
        return [d async for d in self.sessions.find({})]

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    DEFAULT_SETTINGS = {
        "threads": config.DEFAULT_THREADS,
        "forward_delay": config.DEFAULT_FORWARD_DELAY,
        "retry_count": 3,
        "handle_flood": True,
        "auto_resume": True,
        "notifications": True,
        "dark_theme": False,
    }

    async def get_settings(self, user_id: int) -> dict:
        doc = await self.settings.find_one({"user_id": user_id})
        if not doc:
            return {**self.DEFAULT_SETTINGS, "user_id": user_id}
        merged = {**self.DEFAULT_SETTINGS}
        merged.update({k: v for k, v in doc.items() if k in self.DEFAULT_SETTINGS})
        merged["user_id"] = user_id
        return merged

    async def set_setting(self, user_id: int, key: str, value: Any) -> None:
        await self.settings.update_one(
            {"user_id": user_id}, {"$set": {key: value}}, upsert=True
        )

    # ------------------------------------------------------------------
    # jobs
    # ------------------------------------------------------------------
    async def save_job(self, job: dict) -> str:
        job = dict(job)
        job.setdefault("created_at", now())
        job.setdefault("status", "saved")  # saved | scheduled | running | done
        res = await self.jobs.insert_one(job)
        return str(res.inserted_id)

    async def get_job(self, jid: str) -> dict | None:
        from bson import ObjectId

        try:
            return await self.jobs.find_one({"_id": ObjectId(jid)})
        except Exception:
            return None

    async def user_jobs(self, user_id: int) -> list[dict]:
        return [
            d async for d in self.jobs.find({"user_id": user_id}).sort("created_at", -1)
        ]

    async def delete_job(self, user_id: int, jid: str) -> None:
        from bson import ObjectId

        await self.jobs.delete_one({"_id": ObjectId(jid), "user_id": user_id})

    async def update_job(self, jid: str, patch: dict) -> None:
        from bson import ObjectId

        await self.jobs.update_one({"_id": ObjectId(jid)}, {"$set": patch})

    async def due_jobs(self, ts: datetime) -> list[dict]:
        return [
            d
            async for d in self.jobs.find(
                {
                    "schedule_kind": {"$in": ["later", "daily", "weekly"]},
                    "status": "scheduled",
                    "next_run": {"$lte": ts},
                }
            )
        ]

    async def mark_running(self, jid: str) -> None:
        await self.update_job(jid, {"status": "running", "started_at": now()})

    # ------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------
    async def add_log(self, log_doc: dict) -> str:
        log_doc.setdefault("started_at", now())
        res = await self.logs.insert_one(log_doc)
        return str(res.inserted_id)

    async def user_logs(self, user_id: int, limit: int = 10) -> list[dict]:
        return [
            d
            async for d in self.logs.find({"user_id": user_id}).sort("started_at", -1).limit(limit)
        ]

    async def recent_logs(self, limit: int = 10) -> list[dict]:
        return [d async for d in self.logs.find({}).sort("started_at", -1).limit(limit)]

    async def update_log(self, log_id, patch: dict) -> None:
        from bson import ObjectId

        await self.logs.update_one({"_id": ObjectId(str(log_id))}, {"$set": patch})

    # ------------------------------------------------------------------
    # transferred_messages (dedup)
    # ------------------------------------------------------------------
    async def is_transferred(self, source_id: int, dest_id: int, msg_id: int) -> bool:
        return await self.transferred.find_one(
            {"source_id": source_id, "dest_id": dest_id, "msg_id": msg_id}
        ) is not None

    async def mark_transferred(
        self,
        source_id: int,
        dest_id: int,
        msg_id: int,
        sid: str,
        mode: str,
    ) -> None:
        try:
            await self.transferred.insert_one(
                {
                    "source_id": source_id,
                    "dest_id": dest_id,
                    "msg_id": msg_id,
                    "sid": sid,
                    "mode": mode,
                    "transferred_at": now(),
                }
            )
        except Exception:
            # duplicate key -> already marked
            pass

    async def clear_transferred(self, user_id: int) -> int:
        """Delete the dedup records for a user's accounts.

        Returns the number of records removed. Used by /cleanup so messages
        are copied again on the next transfer.
        """
        sids = [d["sid"] for d in await self.get_user_sessions(user_id)]
        if not sids:
            return 0
        res = await self.transferred.delete_many({"sid": {"$in": sids}})
        return res.deleted_count

    async def transferred_count(self, user_id: int | None = None) -> int:
        q = {"sid": {"$in": [d["sid"] for d in await self.get_user_sessions(user_id)]}} if user_id else {}
        return await self.transferred.count_documents(q)

    async def transferred_today(self, user_id: int | None = None) -> int:
        since = now() - timedelta(days=1)
        q = {"transferred_at": {"$gte": since}}
        if user_id:
            q["sid"] = {"$in": [d["sid"] for d in await self.get_user_sessions(user_id)]}
        return await self.transferred.count_documents(q)

    async def transferred_by_mode(self, user_id: int | None = None) -> list[dict]:
        match: dict[str, Any] = {}
        if user_id:
            match["sid"] = {"$in": [d["sid"] for d in await self.get_user_sessions(user_id)]}
        pipeline: list[dict[str, Any]] = [
            {"$match": match},
            {"$group": {"_id": "$mode", "count": {"$sum": 1}}},
        ]
        return [d async for d in self.transferred.aggregate(pipeline)]

    async def stats_by_user(self) -> list[dict]:
        pipeline = [
            {"$group": {"_id": "$sid", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
        ]
        return [d async for d in self.transferred.aggregate(pipeline)]


db = Database()
