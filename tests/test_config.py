import pytest
from app.config import Settings

def test_database_name_is_fixed(monkeypatch):
    values={'BOT_TOKEN':'x','API_ID':'1','API_HASH':'x','MONGO_URI':'mongodb://localhost','OWNER_ID':'1','SESSION_ENCRYPTION_KEY':'x','APP_SECRET':'x'*40,'MONGO_DB_NAME':'other'}
    for k,v in values.items(): monkeypatch.setenv(k,v)
    with pytest.raises(RuntimeError): Settings.from_env()


def test_app_secret_requires_sufficient_entropy(monkeypatch):
    values={'BOT_TOKEN':'x','API_ID':'1','API_HASH':'x','MONGO_URI':'mongodb://localhost','OWNER_ID':'1','SESSION_ENCRYPTION_KEY':'x','APP_SECRET':'short'}
    for k,v in values.items(): monkeypatch.setenv(k,v)
    monkeypatch.setenv('MONGO_DB_NAME','jarvis_hyperlink_index')
    with pytest.raises(RuntimeError, match='APP_SECRET'):
        Settings.from_env()
