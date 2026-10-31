"""Encryption helpers for Telegram user sessions."""
from cryptography.fernet import Fernet, InvalidToken

class SessionCipher:
    def __init__(self, key: str):
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Stored Telegram session cannot be decrypted; check SESSION_ENCRYPTION_KEY") from exc

    def encrypt_for_user(self, user_id: int, value: str) -> str:
        return self.encrypt(f"jarvis-session-v1:{int(user_id)}:{value}")

    def decrypt_for_user(self, user_id: int, value: str) -> str:
        decoded = self.decrypt(value)
        prefix = f"jarvis-session-v1:{int(user_id)}:"
        # Legacy ciphertexts are accepted once for backward compatibility; callers
        # should immediately re-encrypt them with encrypt_for_user.
        if decoded.startswith("jarvis-session-v1:"):
            if not decoded.startswith(prefix):
                raise ValueError("Stored Telegram session belongs to a different user")
            return decoded[len(prefix):]
        return decoded
