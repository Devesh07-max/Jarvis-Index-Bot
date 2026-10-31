"""Mongo-backed aiogram FSM storage. Never stores OTPs or 2FA passwords."""
from datetime import datetime, timedelta, timezone
from aiogram.fsm.storage.base import BaseStorage, StorageKey

class MongoFSMStorage(BaseStorage):
    def __init__(self, collection, ttl_seconds: int = 7 * 24 * 3600):
        self.collection = collection
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _key(key: StorageKey) -> str:
        return ":".join(str(x) for x in (key.bot_id, key.chat_id, key.user_id, key.thread_id or 0, key.business_connection_id or "", key.destiny))

    async def set_state(self, key: StorageKey, state: str | None = None):
        storage_key = self._key(key)
        now = datetime.now(timezone.utc)
        if state is None:
            # Aiogram may clear only the state while retaining workflow data
            # (e.g. validated channel awaiting the Register button).
            await self.collection.update_one({"storage_key": storage_key}, {"$set": {
                "storage_key": storage_key, "state": None, "updated_at": now,
                "expires_at": now + timedelta(seconds=self.ttl_seconds)}}, upsert=True)
            return
        await self.collection.update_one({"storage_key": storage_key}, {"$set": {
            "storage_key": storage_key, "bot_id": key.bot_id, "chat_id": key.chat_id,
            "user_id": key.user_id, "state": state, "updated_at": now,
            "expires_at": now + timedelta(seconds=self.ttl_seconds)}, "$setOnInsert": {"data": {}}}, upsert=True)

    async def get_state(self, key: StorageKey) -> str | None:
        row = await self.collection.find_one({"storage_key": self._key(key), "expires_at": {"$gt": datetime.now(timezone.utc)}})
        return row.get("state") if row else None

    async def set_data(self, key: StorageKey, data: dict):
        storage_key = self._key(key); now = datetime.now(timezone.utc)
        if not data:
            await self.collection.update_one({"storage_key": storage_key}, {"$set": {"data": {}, "updated_at": now, "expires_at": now + timedelta(seconds=self.ttl_seconds)}}, upsert=True)
            return
        await self.collection.update_one({"storage_key": storage_key}, {"$set": {
            "storage_key": storage_key, "data": data, "updated_at": now,
            "expires_at": now + timedelta(seconds=self.ttl_seconds)}}, upsert=True)

    async def get_data(self, key: StorageKey) -> dict:
        row = await self.collection.find_one({"storage_key": self._key(key), "expires_at": {"$gt": datetime.now(timezone.utc)}})
        return dict(row.get("data") or {}) if row else {}

    async def close(self):
        return None
