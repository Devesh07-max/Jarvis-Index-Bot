from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING

class Database:
    """MongoDB boundary locked to the dedicated JARVIS database."""
    ALLOWED_DB = "jarvis_hyperlink_index"

    def __init__(self, uri: str, db_name: str):
        if db_name != self.ALLOWED_DB:
            raise ValueError(f"Refusing to access a database other than {self.ALLOWED_DB}")
        self.client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000, retryWrites=True)
        self.db = self.client[db_name]

    def __getattr__(self, name: str):
        # Bot handlers access collections as db.indexing_jobs, while the
        # wrapper owns the Motor database in self.db.
        if name.startswith("_"):
            raise AttributeError(name)
        return self.db[name]

    async def initialize(self):
        await self.client.admin.command("ping")
        await self.db.users.create_index([("user_id", ASCENDING)], unique=True)
        await self.db.telegram_sessions.create_index([("user_id", ASCENDING)], unique=True)
        await self.db.registered_channels.create_index([("user_id", ASCENDING), ("channel_id", ASCENDING)], unique=True)
        await self.db.index_templates.create_index([("user_id", ASCENDING), ("channel_id", ASCENDING)], unique=True)
        await self.db.indexing_jobs.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
        await self.db.indexing_jobs.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
        await self.db.indexing_jobs.create_index([("user_id", ASCENDING), ("job_id", ASCENDING)], unique=True)
        await self.db.indexing_jobs.create_index([("lease_until", ASCENDING)])
        await self.db.indexing_jobs.create_index([("active_key", ASCENDING)], unique=True, sparse=True)
        await self.db.manual_resolutions.create_index([("user_id", ASCENDING), ("channel_id", ASCENDING), ("entry_key", ASCENDING)], unique=True)
        await self.db.previews.create_index([("user_id", ASCENDING), ("job_id", ASCENDING)], unique=True)
        await self.db.previews.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
        await self.db.premium_subscriptions.create_index([("user_id", ASCENDING)], unique=True)
        await self.db.fsm_states.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
        await self.db.fsm_states.create_index([("storage_key", ASCENDING)], unique=True)
        await self.db.login_attempts.create_index([("user_id", ASCENDING)], unique=True)
        await self.db.login_attempts.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
        await self.db.publish_holds.create_index([("user_id", ASCENDING), ("channel_id", ASCENDING)], unique=True)

    async def user_page(self, collection_name: str, user_id: int, page: int = 0, page_size: int = 8, sort_field: str = "created_at", descending: bool = True):
        """Database-side pagination; never loads an entire user's collection."""
        collection = self.db[collection_name]
        total = await collection.count_documents({"user_id": int(user_id)})
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(0, min(int(page), pages - 1))
        direction = DESCENDING if descending else ASCENDING
        rows = await collection.find({"user_id": int(user_id)}).sort(sort_field, direction).skip(page * page_size).limit(page_size).to_list(page_size)
        return rows, page, pages, total

    def close(self):
        self.client.close()
