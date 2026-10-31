"""Exercise Mongo FSM semantics without installing aiogram in the offline test env."""
import importlib
import sys
import types
from dataclasses import dataclass
import pytest


@dataclass
class StorageKey:
    bot_id: int
    chat_id: int
    user_id: int
    thread_id: int | None = None
    business_connection_id: str | None = None
    destiny: str = "default"


class BaseStorage:
    pass


class FakeCollection:
    def __init__(self): self.rows={}
    async def update_one(self, query, update, upsert=False):
        key=query["storage_key"]
        row=self.rows.setdefault(key,{})
        row.update(update.get("$set",{}))
        for name in update.get("$unset",{}): row.pop(name,None)
        return types.SimpleNamespace(modified_count=1)
    async def find_one(self, query):
        row=self.rows.get(query["storage_key"])
        if not row: return None
        if row.get("expires_at") <= query["expires_at"]["$gt"] if isinstance(query.get("expires_at"),dict) else False: return None
        return dict(row)
    async def delete_one(self, query):
        self.rows.pop(query["storage_key"],None)


@pytest.mark.asyncio
async def test_set_state_none_preserves_nonsecret_workflow_data(monkeypatch):
    base=types.ModuleType("aiogram")
    fsm=types.ModuleType("aiogram.fsm")
    storage=types.ModuleType("aiogram.fsm.storage")
    base_module=types.ModuleType("aiogram.fsm.storage.base")
    base_module.BaseStorage=BaseStorage
    base_module.StorageKey=StorageKey
    monkeypatch.setitem(sys.modules,"aiogram",base)
    monkeypatch.setitem(sys.modules,"aiogram.fsm",fsm)
    monkeypatch.setitem(sys.modules,"aiogram.fsm.storage",storage)
    monkeypatch.setitem(sys.modules,"aiogram.fsm.storage.base",base_module)
    sys.modules.pop("app.fsm_storage",None)
    module=importlib.import_module("app.fsm_storage")
    collection=FakeCollection(); store=module.MongoFSMStorage(collection)
    key=StorageKey(1,2,3)
    await store.set_data(key,{"pending_channel":{"channel_id":-100123}})
    await store.set_state(key,"Flow:channel")
    await store.set_state(key,None)
    assert await store.get_state(key) is None
    assert await store.get_data(key)=={"pending_channel":{"channel_id":-100123}}
    await store.set_state(key,None)
    await store.set_data(key,{})
    assert await store.get_data(key)=={}
    sys.modules.pop("app.fsm_storage",None)
