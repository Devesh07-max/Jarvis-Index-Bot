from datetime import datetime, timedelta, timezone

class Premium:
    def __init__(self, db):
        self.db = db

    async def grant(self, user_id: int, days: int):
        if int(days) < 1 or int(days) > 36500:
            raise ValueError("Days must be between 1 and 36500")
        now = datetime.now(timezone.utc)
        # Atomic Mongo update prevents concurrent grants overwriting one another.
        pipeline = [{"$set": {
            "user_id": int(user_id),
            "expires_at": {"$dateAdd": {
                "startDate": {"$cond": [{"$gt": ["$expires_at", now]}, "$expires_at", now]},
                "unit": "day", "amount": int(days)}},
            "active": True,
            "updated_at": now,
        }}]
        await self.db.premium_subscriptions.update_one({"user_id": int(user_id)}, pipeline, upsert=True)
        row = await self.db.premium_subscriptions.find_one({"user_id": int(user_id)})
        return row["expires_at"]

    async def revoke(self, user_id: int):
        await self.db.premium_subscriptions.update_one({"user_id": int(user_id)}, {"$set": {
            "active": False, "expires_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)}})

    async def active(self, user_id: int):
        now = datetime.now(timezone.utc)
        row = await self.db.premium_subscriptions.find_one({"user_id": int(user_id), "active": True, "expires_at": {"$gt": now}})
        if row:
            return True
        await self.db.premium_subscriptions.update_one(
            {"user_id": int(user_id), "active": True, "expires_at": {"$lte": now}},
            {"$set": {"active": False, "updated_at": now}})
        return False

    async def expire_due(self):
        now = datetime.now(timezone.utc)
        result = await self.db.premium_subscriptions.update_many(
            {"active": True, "expires_at": {"$lte": now}},
            {"$set": {"active": False, "updated_at": now}})
        return result.modified_count

    async def active_page(self, page=0, page_size=50):
        now = datetime.now(timezone.utc)
        query = {"active": True, "expires_at": {"$gt": now}}
        total = await self.db.premium_subscriptions.count_documents(query)
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(0, min(int(page), pages - 1))
        rows = await self.db.premium_subscriptions.find(query).sort("expires_at", 1).skip(page * page_size).limit(page_size).to_list(page_size)
        return rows, page, pages, total
