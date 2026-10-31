from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_id: int
    api_hash: str
    mongo_uri: str
    mongo_db_name: str
    owner_id: int
    session_encryption_key: str
    app_secret: str
    public_app_url: str
    port: int

    @classmethod
    def from_env(cls):
        required = ["BOT_TOKEN", "API_ID", "API_HASH", "MONGO_URI", "OWNER_ID", "SESSION_ENCRYPTION_KEY", "APP_SECRET"]
        missing = [k for k in required if not os.getenv(k)]
        if missing:
            raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
        if len(os.environ["APP_SECRET"].encode()) < 32:
            raise RuntimeError("APP_SECRET must contain at least 32 bytes")
        if int(os.environ["API_ID"]) <= 0 or int(os.environ["OWNER_ID"]) <= 0:
            raise RuntimeError("API_ID and OWNER_ID must be positive integers")
        db_name = os.getenv("MONGO_DB_NAME", "jarvis_hyperlink_index")
        if db_name != "jarvis_hyperlink_index":
            raise RuntimeError("MONGO_DB_NAME must be jarvis_hyperlink_index; refusing unexpected database")
        return cls(os.environ["BOT_TOKEN"], int(os.environ["API_ID"]), os.environ["API_HASH"],
                   os.environ["MONGO_URI"], db_name, int(os.environ["OWNER_ID"]),
                   os.environ["SESSION_ENCRYPTION_KEY"], os.environ["APP_SECRET"],
                   os.getenv("PUBLIC_APP_URL", ""), int(os.getenv("PORT", "10000")))
