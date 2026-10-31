"""Per-user Telethon session management and safe channel-history access."""
from __future__ import annotations
import asyncio
import time
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import Channel

class UserTelegram:
    PENDING_TTL_SECONDS = 600

    def __init__(self, api_id: int, api_hash: str, cipher, database):
        self.api_id, self.api_hash, self.cipher, self.db = api_id, api_hash, cipher, database
        self.clients: dict[int, TelegramClient] = {}
        self.pending: dict[int, dict] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(int(user_id), asyncio.Lock())

    async def _close_pending_unlocked(self, user_id: int):
        pending = self.pending.pop(int(user_id), None)
        if pending:
            try:
                await pending["client"].disconnect()
            except Exception:
                pass

    async def cancel_login(self, user_id: int):
        async with self._lock(user_id):
            await self._close_pending_unlocked(user_id)

    async def cleanup_expired_pending(self):
        now = time.monotonic()
        expired = [(uid, item) for uid, item in list(self.pending.items())
                   if now - item.get("created_monotonic", now) > self.PENDING_TTL_SECONDS]
        cleaned = 0
        for uid, item in expired:
            async with self._lock(uid):
                if self.pending.get(uid) is item:
                    await self._close_pending_unlocked(uid)
                    cleaned += 1
        return cleaned

    async def client_for(self, user_id: int):
        uid = int(user_id)
        async with self._lock(uid):
            existing = self.clients.get(uid)
            if existing and existing.is_connected():
                try:
                    authorized = await existing.is_user_authorized()
                except Exception as exc:
                    # A transient network/FloodWait error is not evidence that a
                    # stored session was revoked. Preserve it and surface the error.
                    if type(exc).__name__ not in {"AuthKeyUnregisteredError", "AuthKeyDuplicatedError", "SessionRevokedError", "UserDeactivatedError", "UserDeactivatedBanError"}:
                        raise
                    authorized = False
                if authorized:
                    return existing
                self.clients.pop(uid, None)
                try: await existing.disconnect()
                except Exception: pass
                await self.db.telegram_sessions.delete_one({"user_id": uid})
                return None
            row = await self.db.telegram_sessions.find_one({"user_id": uid})
            if not row:
                return None
            session = self.cipher.decrypt_for_user(uid, row["session"])
            client = TelegramClient(StringSession(session), self.api_id, self.api_hash)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    await self.db.telegram_sessions.delete_one({"user_id": uid})
                    return None
                # Upgrade legacy encrypted sessions to user-bound payloads.
                if not self.cipher.decrypt(row["session"]).startswith("jarvis-session-v1:"):
                    await self.db.telegram_sessions.update_one({"user_id": uid}, {"$set": {"session": self.cipher.encrypt_for_user(uid, session)}})
                self.clients[uid] = client
                return client
            except Exception:
                await client.disconnect()
                raise

    async def begin_login(self, user_id: int, phone: str):
        uid = int(user_id)
        async with self._lock(uid):
            if await self.db.telegram_sessions.find_one({"user_id": uid}):
                raise RuntimeError("An account is already connected")
            await self._close_pending_unlocked(uid)
            # Persistent per-user throttling across restarts (three OTP requests/hour).
            now = datetime.now(timezone.utc)
            attempt = await self.db.login_attempts.find_one({"user_id": uid})
            window_started = attempt.get("window_started", now) if attempt else now
            if getattr(window_started, "tzinfo", None) is None:
                window_started = window_started.replace(tzinfo=timezone.utc)
            if attempt and (now - window_started).total_seconds() < 3600 and int(attempt.get("count", 0)) >= 3:
                raise RuntimeError("Too many login attempts; try again later")
            if not attempt or (now - window_started).total_seconds() >= 3600:
                await self.db.login_attempts.update_one({"user_id": uid}, {"$set": {"user_id": uid, "count": 1, "window_started": now, "expires_at": now + timedelta(hours=2)}}, upsert=True)
            else:
                await self.db.login_attempts.update_one({"user_id": uid}, {"$inc": {"count": 1}, "$set": {"expires_at": now + timedelta(hours=2)}})
            client = TelegramClient(StringSession(), self.api_id, self.api_hash)
            try:
                await client.connect()
                sent = await client.send_code_request(phone)
            except Exception:
                await client.disconnect()
                raise
            self.pending[uid] = {"client": client, "phone": phone,
                                 "phone_code_hash": sent.phone_code_hash,
                                 "created_monotonic": time.monotonic(), "needs_password": False, "attempts": 0}

    async def finish_login(self, user_id: int, code: str, password: str | None = None):
        uid = int(user_id)
        async with self._lock(uid):
            state = self.pending.get(uid)
            if not state:
                raise RuntimeError("Login expired; start again")
            if time.monotonic() - state["created_monotonic"] > self.PENDING_TTL_SECONDS:
                await self._close_pending_unlocked(uid)
                raise RuntimeError("Login expired; start again")
            client = state["client"]
            try:
                if state.get("needs_password"):
                    if not password:
                        return "2FA_REQUIRED"
                    await client.sign_in(password=password)
                else:
                    await client.sign_in(state["phone"], code, phone_code_hash=state["phone_code_hash"])
            except SessionPasswordNeededError:
                state["needs_password"] = True
                if not password:
                    return "2FA_REQUIRED"
                try:
                    await client.sign_in(password=password)
                except Exception:
                    state["attempts"] = int(state.get("attempts", 0)) + 1
                    if state["attempts"] >= 5:
                        await self._close_pending_unlocked(uid)
                    raise
            except Exception:
                state["attempts"] = int(state.get("attempts", 0)) + 1
                if state["attempts"] >= 5:
                    await self._close_pending_unlocked(uid)
                raise
            try:
                me = await client.get_me()
                session = client.session.save()
                await self.db.telegram_sessions.replace_one({"user_id": uid}, {
                    "user_id": uid, "session": self.cipher.encrypt_for_user(uid, session),
                    "telegram_id": me.id, "username": me.username,
                    "connected_at": datetime.now(timezone.utc)}, upsert=True)
            except Exception:
                # Authentication succeeded but durable storage did not. Close the
                # in-memory client and require a clean retry; never report success.
                await self._close_pending_unlocked(uid)
                raise
            self.clients[uid] = client
            self.pending.pop(uid, None)
            return me

    async def disconnect_user(self, user_id: int):
        uid = int(user_id)
        revoked = True
        async with self._lock(uid):
            client = self.clients.pop(uid, None)
            if client is None:
                # Load the persisted session so Disconnect revokes the Telegram
                # authorization even when this worker has not used it yet.
                row = await self.db.telegram_sessions.find_one({"user_id": uid})
                if row:
                    try:
                        session = self.cipher.decrypt_for_user(uid, row["session"])
                        client = TelegramClient(StringSession(session), self.api_id, self.api_hash)
                        await client.connect()
                    except Exception:
                        if client:
                            try: await client.disconnect()
                            except Exception: pass
                        client = None
                        revoked = False
            if client:
                try:
                    if await client.is_user_authorized():
                        await client.log_out()
                    else:
                        await client.disconnect()
                except Exception:
                    revoked = False
                    try: await client.disconnect()
                    except Exception: pass
            await self._close_pending_unlocked(uid)
            await self.db.telegram_sessions.delete_one({"user_id": uid})
        return revoked

    @staticmethod
    def marked_channel_id(entity) -> int:
        return int(f"-100{entity.id}")

    @staticmethod
    def _known_message_validation_error(exc) -> bool:
        return type(exc).__name__ in {
            "ChannelPrivateError", "ChannelInvalidError", "PeerIdInvalidError",
            "MessageIdInvalidError", "UsernameInvalidError", "UsernameNotOccupiedError",
            "InviteHashInvalidError", "InviteHashExpiredError",
        }

    async def get_channels(self, user_id: int):
        client = await self.client_for(user_id)
        if not client:
            raise RuntimeError("Connect your Telegram account first")
        result = []
        async for dialog in client.iter_dialogs():
            if dialog.is_channel:
                result.append({"id": self.marked_channel_id(dialog.entity), "title": dialog.title,
                               "username": getattr(dialog.entity, "username", None)})
        return result

    async def resolve_channel(self, user_id: int, reference: str):
        client = await self.client_for(user_id)
        if not client:
            raise RuntimeError("Connect your Telegram account first")
        ref = reference.strip()
        if ref.startswith("@"):
            ref = ref[1:]
        if ref.startswith(("https://", "http://")):
            from urllib.parse import urlparse
            parsed=urlparse(ref)
            if parsed.netloc.lower() not in {"t.me","www.t.me","telegram.me","www.telegram.me"}:
                raise ValueError("Only Telegram channel references are supported")
            parts=[part for part in parsed.path.split("/") if part]
            if parts and parts[0]=="s": parts=parts[1:]
            if len(parts)>=2 and parts[0]=="c" and parts[1].isdigit():
                ref=int(f"-100{parts[1]}")
            elif parts and parts[0] not in {"joinchat"} and not parts[0].startswith("+"):
                ref=parts[0]
            else:
                raise ValueError("Private invite links must be joined first; provide the accessible channel ID")
        if isinstance(ref,str) and ref.isdigit():
            ref=int(f"-100{ref}")
        elif isinstance(ref,str) and ref.lstrip("-").isdigit():
            ref=int(ref)
        entity = await client.get_entity(ref)
        if not isinstance(entity, Channel):
            raise ValueError("The reference is not a Telegram channel")
        return entity

    async def iter_channel_messages(self, user_id: int, channel_id: int, *, min_id=0, progress=None):
        client = await self.client_for(user_id)
        if not client:
            raise RuntimeError("Telegram account is disconnected")
        from .indexer import MessageRecord
        count, last_id = 0, int(min_id or 0)
        while True:
            try:
                entity = await client.get_entity(int(channel_id))
                async for msg in client.iter_messages(entity, reverse=True, min_id=last_id):
                    last_id = max(last_id, int(msg.id)); count += 1
                    media = getattr(msg, "media", None)
                    document = getattr(media, "document", None) if media else None
                    filename = None; mime_type = None
                    if document:
                        mime_type = getattr(document, "mime_type", None)
                        for attr in getattr(document, "attributes", []) or []:
                            name = getattr(attr, "file_name", None)
                            if name:
                                filename = name
                                break
                    if msg.message or media or filename:
                        yield MessageRecord(msg.id, msg.message or "", msg.date, filename, mime_type)
                    if progress and count % 100 == 0:
                        await progress(count, last_id)
                break
            except FloodWaitError as exc:
                remaining=max(1,int(exc.seconds))
                while remaining > 0:
                    pause=min(5,remaining)
                    await asyncio.sleep(pause); remaining-=pause
                    if progress: await progress(count,last_id)
        if progress:
            await progress(count, last_id)

    async def scan(self, user_id: int, channel_id: int, progress=None, min_id=0):
        messages = []
        async for record in self.iter_channel_messages(user_id, channel_id, min_id=min_id, progress=progress):
            messages.append(record)
        return messages, len(messages)

    async def can_access_channel(self, user_id: int, channel_id: int) -> bool:
        client = await self.client_for(user_id)
        if not client: return False
        try:
            entity = await client.get_entity(int(channel_id))
            return isinstance(entity, Channel)
        except Exception as exc:
            if self._known_message_validation_error(exc):
                return False
            raise

    async def validate_message_link(self, user_id: int, channel_id: int, link: str):
        import re
        match = re.fullmatch(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:c/)?([A-Za-z0-9_+-]+)/(?:(?:\d+)/)?(\d+)(?:\?.*)?", (link or "").strip())
        if not match:
            return None
        channel_ref, message_id = match.group(1), int(match.group(2))
        client = await self.client_for(user_id)
        if not client:
            return None
        try:
            if channel_ref.isdigit():
                entity = await client.get_entity(int(f"-100{channel_ref}"))
            else:
                entity = await client.get_entity(channel_ref)
            actual_id = self.marked_channel_id(entity) if isinstance(entity, Channel) else None
            if actual_id != int(channel_id):
                return None
            msg = await client.get_messages(entity, ids=message_id)
            valid = bool(msg and type(msg).__name__ != "MessageEmpty" and not getattr(msg, "empty", False))
            return message_id if valid else None
        except Exception as exc:
            if self._known_message_validation_error(exc):
                return None
            raise

    async def validate_message(self, user_id: int, channel_id: int, message_id: int):
        client = await self.client_for(user_id)
        if not client:
            return False
        try:
            entity = await client.get_entity(int(channel_id))
            msg = await client.get_messages(entity, ids=message_id)
            return bool(msg and type(msg).__name__ != "MessageEmpty" and not getattr(msg, "empty", False))
        except Exception as exc:
            if self._known_message_validation_error(exc):
                return False
            raise

    async def close(self):
        for uid in list(self.pending):
            await self.cancel_login(uid)
        for uid, client in list(self.clients.items()):
            try:
                await client.disconnect()
            except Exception:
                pass
        self.clients.clear()
