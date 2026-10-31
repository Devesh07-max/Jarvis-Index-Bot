"""Mongo-backed durable indexing job state transitions and checkpoints."""
from datetime import datetime, timezone
try:
    from pymongo import ASCENDING, ReturnDocument
except ImportError:  # dependency-light unit tests; production installs pymongo via Motor
    ASCENDING = 1
    class ReturnDocument:
        AFTER = True

ACTIVE = ("Queued", "Scanning")

class JobStore:
    def __init__(self, collection):
        self.collection = collection

    async def ensure_indexes(self):
        await self.collection.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
        await self.collection.create_index([("user_id", ASCENDING), ("job_id", ASCENDING)], unique=True)

    async def enqueue(self, *, user_id, job_id, channel_id, channel_title, mode, template="", chat_id=None):
        now = datetime.now(timezone.utc)
        doc = {"user_id": int(user_id), "job_id": job_id, "channel_id": int(channel_id),
               "channel_title": channel_title, "mode": mode, "template": template,
               "chat_id": chat_id, "status": "Queued", "active_key": f"{int(user_id)}:{int(channel_id)}", "checkpoint_last_id": 0,
               "messages_scanned": 0, "cancel_requested": False,
               "created_at": now, "updated_at": now, "attempts": 0}
        await self.collection.insert_one(doc)
        return doc

    async def recover_interrupted(self):
        result = await self.collection.update_many(
            {"status": "Scanning", "cancel_requested": {"$ne": True}},
            {"$set": {"status": "Queued", "updated_at": datetime.now(timezone.utc)}})
        return result.modified_count

    async def claim(self, job_id=None, user_id=None, worker_id=None):
        query = {"status": "Queued", "cancel_requested": {"$ne": True}}
        if job_id is not None: query["job_id"] = job_id
        if user_id is not None: query["user_id"] = int(user_id)
        update = {"$set": {"status": "Scanning", "updated_at": datetime.now(timezone.utc)}, "$inc": {"attempts": 1}}
        if worker_id: update["$set"]["worker_id"] = worker_id
        return await self.collection.find_one_and_update(query, update, sort=[("created_at", ASCENDING)], return_document=ReturnDocument.AFTER)

    async def checkpoint(self, user_id, job_id, last_id, scanned, *, builder_state=None):
        fields = {"checkpoint_last_id": int(last_id), "messages_scanned": int(scanned), "updated_at": datetime.now(timezone.utc)}
        if builder_state is not None: fields["builder_state"] = builder_state
        return await self.collection.update_one(
            {"user_id": int(user_id), "job_id": job_id, "status": "Scanning", "cancel_requested": {"$ne": True}},
            {"$set": fields})

    async def request_cancel(self, user_id, job_id):
        return await self.collection.update_one(
            {"user_id": int(user_id), "job_id": job_id, "status": {"$in": list(ACTIVE)}},
            {"$set": {"cancel_requested": True, "status": "Cancelled", "updated_at": datetime.now(timezone.utc)}, "$unset": {"active_key": ""}})

    async def finish(self, user_id, job_id, status, **fields):
        if status not in {"Preview Ready", "Waiting for Resolution", "Failed", "Cancelled"}:
            raise ValueError("Invalid terminal job status")
        fields.update(status=status, updated_at=datetime.now(timezone.utc))
        return await self.collection.update_one(
            {"user_id": int(user_id), "job_id": job_id, "status": {"$in": list(ACTIVE)}},
            {"$set": fields, "$unset": {"active_key": ""}})
