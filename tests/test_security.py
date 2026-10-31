from cryptography.fernet import Fernet
from app.security import SessionCipher

def test_session_encryption_roundtrip():
    cipher=SessionCipher(Fernet.generate_key().decode())
    encrypted=cipher.encrypt('telethon-session-string')
    assert encrypted!='telethon-session-string'
    assert cipher.decrypt(encrypted)=='telethon-session-string'
