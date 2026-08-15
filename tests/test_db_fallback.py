import asyncio

import pytest
from pymongo.errors import ServerSelectionTimeoutError

from bot import db as db_module


class FailingClient:
    def __init__(self, *args, **kwargs):
        raise ServerSelectionTimeoutError("no mongo")


def test_init_falls_back_to_in_memory_when_mongo_unavailable(monkeypatch):
    monkeypatch.setattr(db_module, "AsyncIOMotorClient", FailingClient)

    async def run_test():
        database = db_module.Database()
        await database.init()
        await database.set_setting(42, "threads", 8)
        settings = await database.get_settings(42)
        assert settings["threads"] == 8

    asyncio.run(run_test())
