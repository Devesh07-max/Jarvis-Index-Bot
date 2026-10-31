"""Atomic publishing transitions and signed, expiring second confirmations."""
from __future__ import annotations
import hashlib
import hmac
from datetime import datetime, timezone


def approval_signature(secret: str, user_id: int, job_id: str, nonce: str) -> str:
    message = f"{int(user_id)}:{job_id}:{nonce}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()[:24]


def confirmation_matches(secret: str, signature: str, user_id: int, job_id: str, nonce: str) -> bool:
    return bool(signature and nonce and hmac.compare_digest(
        signature, approval_signature(secret, user_id, job_id, nonce)))


async def claim_publish(collection, user_id: int, job_id: str, nonce: str, now: datetime) -> bool:
    result = await collection.update_one(
        {"user_id": int(user_id), "job_id": job_id, "status": "Preview Ready",
         "approval_token": nonce, "approval_expires_at": {"$gt": now}, "publishing": {"$ne": True}},
        {"$set": {"status": "Publishing", "publishing": True, "publish_started_at": now,
                  "publish_chunks": [], "publish_chunk_index": 0, "publish_inflight_index": None},
         "$unset": {"approval_token": "", "approval_expires_at": ""}})
    return result.modified_count == 1


async def mark_chunk_inflight(collection, user_id: int, job_id: str, index: int):
    return await collection.update_one(
        {"user_id": int(user_id), "job_id": job_id, "status": "Publishing"},
        {"$set": {"publish_inflight_index": int(index)}})


async def clear_chunk_inflight(collection, user_id: int, job_id: str):
    return await collection.update_one(
        {"user_id": int(user_id), "job_id": job_id, "status": "Publishing"},
        {"$set": {"publish_inflight_index": None}})


async def record_chunk(collection, user_id: int, job_id: str, index: int, message_id: int):
    return await collection.update_one(
        {"user_id": int(user_id), "job_id": job_id, "status": "Publishing"},
        {"$push": {"publish_chunks": {"index": int(index), "message_id": int(message_id)}},
         "$set": {"publish_chunk_index": int(index) + 1, "publish_inflight_index": None}})


async def commit_published(collection, user_id: int, job_id: str, message_ids: list[int], published_at: datetime):
    if not message_ids:
        raise ValueError("Cannot commit an empty publishing result")
    result = await collection.update_one(
        {"user_id": int(user_id), "job_id": job_id, "status": "Publishing"},
        {"$set": {"status": "Published", "publishing": False, "published_at": published_at,
                  "published_message_id": int(message_ids[0]), "published_message_ids": [int(x) for x in message_ids],
                  "publish_inflight_index": None}})
    return result.modified_count == 1


async def mark_publish_failure(collection, user_id: int, job_id: str, *, partial: bool, error: str):
    status = "Publish Review Required" if partial else "Preview Ready"
    fields = {"status": status, "publishing": False, "publish_error": error,
              "updated_at": datetime.now(timezone.utc)}
    if partial:
        fields["publish_review_required"] = True
    return await collection.update_one(
        {"user_id": int(user_id), "job_id": job_id, "status": "Publishing"},
        {"$set": fields})


async def recover_interrupted_publishes(collection):
    now = datetime.now(timezone.utc)
    result = await collection.update_many(
        {"status": "Publishing"},
        {"$set": {"status": "Publish Review Required", "publishing": False,
                  "publish_review_required": True, "publish_recovery_at": now}})
    return result.modified_count
