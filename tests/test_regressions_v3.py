from datetime import datetime, timezone, timedelta
import pytest
from cryptography.fernet import Fernet

from app.indexer import (
    MessageRecord, StreamingIndexBuilder, detect_material, automatic_index,
    match_oldest, render_automatic_index, render_manual_template, IndexEntry,
)
from app.security import SessionCipher
from app.jobs import JobStore
from app.premium import Premium


def test_generic_pdf_is_not_misclassified_as_century_pdf():
    assert detect_material("Anatomy PDF") == "pdfs"
    assert detect_material("Biology lecture PDF") == "lectures"
    assert detect_material("Classification Century PDF") == "century pdf"


def test_specific_material_phrase_wins_over_generic_pdf():
    assert detect_material("Century PDF download") == "century pdf"


def test_filename_metadata_can_drive_category_detection():
    groups, _ = automatic_index([MessageRecord(1, "", filename="Anatomy_Digital_Notes.pdf")])
    assert groups["Anatomy"]["digital notes"] == 1


def test_hindi_caption_subject_and_material_are_detected():
    groups, _ = automatic_index([MessageRecord(1, "वर्गीकरण लेक्चर 01")])
    assert "वर्गीकरण" in groups
    assert groups["वर्गीकरण"]["lectures"] == 1


def test_generic_single_word_manual_match_is_not_accepted():
    assert match_oldest("Tests", [MessageRecord(1, "Latest test schedule")]) is None


def test_manual_matching_uses_plural_normalization_and_oldest_id():
    messages = [MessageRecord(20, "Classification Lecture 02"), MessageRecord(10, "Classification Lecture 01")]
    assert match_oldest("Classification Lectures", messages).message_id == 10


def test_oldest_fallback_uses_telegram_message_id_when_dates_missing():
    assert match_oldest("Anatomy Digital Notes", [
        MessageRecord(90, "Anatomy Digital Notes"), MessageRecord(12, "Anatomy Digital Notes")
    ]).message_id == 12


def test_saved_auto_resolution_survives_builder_regeneration():
    builder = StreamingIndexBuilder("automatic")
    builder.add(MessageRecord(1, "Anatomy Lecture 01"))
    builder.add(MessageRecord(2, "Anatomy Digital Notes"))
    builder.apply_resolutions({"Anatomy digital notes": 777})
    entries, groups, _ = builder.result()
    notes = next(x for x in entries if x.label == "Anatomy digital notes")
    assert notes.message_id == 777
    assert groups["Anatomy"]["digital notes"] == 777


def test_auto_renderer_preserves_subject_grouping_after_resolution():
    entries = [
        IndexEntry("Anatomy lectures", "anatomy::lectures", 1),
        IndexEntry("Anatomy digital notes", "anatomy::digital notes", 777),
    ]
    rendered, missing = render_automatic_index(entries, -100123456)
    assert "<b>Anatomy</b>" in rendered
    assert "https://t.me/c/123456/777" in rendered
    assert missing == []


def test_auto_renderer_escapes_untrusted_subject_and_label():
    rendered, _ = render_automatic_index([IndexEntry("<b>Injected</b> lectures", "injected::lectures", 2)], -1001)
    assert "&lt;b&gt;Injected&lt;/b&gt;" in rendered


def test_manual_template_preserves_unicode_and_escapes_untrusted_text():
    template = "📋 ɴᴀᴠɪɢᴀᴛɪᴏɴ ɪɴᴅᴇx\n\nᴄʟᴀꜱꜱɪꜰɪᴄᴀᴛɪᴏɴ\n├ Classification → Lectures"
    rendered, missing = render_manual_template(template, [IndexEntry("Classification → Lectures", "x", 44)], -100123)
    assert "ᴄʟᴀꜱꜱɪꜰɪᴄᴀᴛɪᴏɴ" in rendered and "├ " in rendered
    assert "https://t.me/c/123/44" in rendered and not missing


def test_builder_snapshot_round_trip_keeps_metadata_derived_categories():
    builder = StreamingIndexBuilder("automatic")
    builder.add(MessageRecord(1, "", filename="Radiology_Planner.pdf"))
    restored = StreamingIndexBuilder("automatic").restore(builder.snapshot())
    entries, groups, _ = restored.result()
    assert groups["Radiology"]["planners"] == 1
    assert entries[0].message_id == 1


def test_user_bound_session_cipher_rejects_cross_user_reuse():
    cipher = SessionCipher(Fernet.generate_key().decode())
    token = cipher.encrypt_for_user(101, "telethon-session")
    assert cipher.decrypt_for_user(101, token) == "telethon-session"
    with pytest.raises(ValueError, match="different user"):
        cipher.decrypt_for_user(202, token)


class FakeCollection:
    def __init__(self):
        self.doc = None
        self.calls = []
    async def create_index(self, *args, **kwargs): self.calls.append(("index", args, kwargs))
    async def insert_one(self, doc): self.doc = dict(doc)
    async def update_many(self, query, update):
        self.calls.append(("many", query, update)); return type("R", (), {"modified_count": 1})()
    async def find_one_and_update(self, query, update, **kwargs):
        self.calls.append(("claim", query, update, kwargs)); return {"job_id": "j", "status": "Scanning"}
    async def update_one(self, query, update, **kwargs):
        self.calls.append(("one", query, update)); return type("R", (), {"modified_count": 1})()


@pytest.mark.asyncio
async def test_job_store_claim_is_user_scoped_and_persists_worker_id():
    collection = FakeCollection(); store = JobStore(collection)
    claimed = await store.claim("j", user_id=123, worker_id="worker-a")
    assert claimed["status"] == "Scanning"
    query = collection.calls[-1][1]
    update = collection.calls[-1][2]
    assert query["user_id"] == 123 and query["job_id"] == "j"
    assert update["$set"]["worker_id"] == "worker-a"


@pytest.mark.asyncio
async def test_job_store_checkpoint_persists_builder_snapshot():
    collection = FakeCollection(); store = JobStore(collection)
    snapshot = {"groups": {"Anatomy": {"lectures": 5}}}
    await store.checkpoint(7, "j", 55, 100, builder_state=snapshot)
    update = collection.calls[-1][2]
    assert update["$set"]["checkpoint_last_id"] == 55
    assert update["$set"]["builder_state"] == snapshot
    assert "$unset" not in update


class FakePremiumCollection:
    def __init__(self, row=None): self.row = row; self.updates = []
    async def update_one(self, query, update, **kwargs): self.updates.append((query, update, kwargs)); return type("R", (), {"modified_count": 1})()
    async def find_one(self, query): return self.row
    async def update_many(self, query, update): self.updates.append((query, update, {})); return type("R", (), {"modified_count": 1})()


@pytest.mark.asyncio
async def test_premium_grant_uses_atomic_date_add_pipeline():
    expiry = datetime.now(timezone.utc) + timedelta(days=30)
    collection = FakePremiumCollection({"expires_at": expiry})
    premium = Premium(type("DB", (), {"premium_subscriptions": collection})())
    assert await premium.grant(7, 30) == expiry
    pipeline = collection.updates[0][1]
    assert isinstance(pipeline, list) and "$dateAdd" in pipeline[0]["$set"]["expires_at"]


@pytest.mark.asyncio
async def test_premium_active_query_excludes_expired_entitlements():
    collection = FakePremiumCollection(None)
    premium = Premium(type("DB", (), {"premium_subscriptions": collection})())
    assert await premium.active(7) is False
    assert collection.updates[-1][0]["expires_at"]["$lte"]


@pytest.mark.asyncio
async def test_premium_grant_rejects_invalid_duration():
    premium = Premium(type("DB", (), {"premium_subscriptions": FakePremiumCollection()})())
    with pytest.raises(ValueError): await premium.grant(7, 0)
    with pytest.raises(ValueError): await premium.grant(7, 36501)


def test_public_channel_render_uses_username_links():
    rendered, missing = render_automatic_index(
        [IndexEntry("Radiology lectures", "radiology::lectures", 88)], -100123, username="radiology_course")
    assert "https://t.me/radiology_course/88" in rendered
    assert "https://t.me/c/123/88" not in rendered
    assert not missing


def test_century_word_without_pdf_is_not_a_century_pdf_category():
    assert detect_material("A century of anatomy lectures") == "lectures"


from app.publishing import (
    approval_signature, confirmation_matches, claim_publish,
    mark_chunk_inflight, record_chunk, commit_published,
    mark_publish_failure, recover_interrupted_publishes,
)


class PublishCollection:
    def __init__(self): self.calls=[]
    async def update_one(self, query, update, **kwargs):
        self.calls.append((query, update)); return type("R", (), {"modified_count": 1})()
    async def update_many(self, query, update, **kwargs):
        self.calls.append((query, update)); return type("R", (), {"modified_count": 2})()


def test_publish_confirmation_signature_is_user_and_job_bound():
    signature = approval_signature("s" * 40, 10, "job-a", "nonce")
    assert confirmation_matches("s" * 40, signature, 10, "job-a", "nonce")
    assert not confirmation_matches("s" * 40, signature, 11, "job-a", "nonce")
    assert not confirmation_matches("s" * 40, signature, 10, "job-b", "nonce")


@pytest.mark.asyncio
async def test_publish_claim_is_single_transition_from_preview_ready():
    collection=PublishCollection(); now=datetime.now(timezone.utc)
    assert await claim_publish(collection, 5, "j", "nonce", now)
    query, update=collection.calls[-1]
    assert query["user_id"]==5 and query["status"]=="Preview Ready"
    assert update["$set"]["status"]=="Publishing"
    assert "approval_token" in update["$unset"]


@pytest.mark.asyncio
async def test_publish_chunk_is_persisted_immediately_and_committed():
    collection=PublishCollection(); now=datetime.now(timezone.utc)
    await mark_chunk_inflight(collection, 5, "j", 0)
    await record_chunk(collection, 5, "j", 0, 88)
    assert collection.calls[-1][1]["$push"]["publish_chunks"]["message_id"]==88
    assert await commit_published(collection, 5, "j", [88, 89], now)
    assert collection.calls[-1][1]["$set"]["published_message_ids"]==[88,89]


@pytest.mark.asyncio
async def test_publish_failure_after_possible_send_requires_manual_review():
    collection=PublishCollection()
    await mark_publish_failure(collection, 5, "j", partial=True, error="NetworkError")
    assert collection.calls[-1][1]["$set"]["status"]=="Publish Review Required"
    assert collection.calls[-1][1]["$set"]["publish_review_required"] is True


@pytest.mark.asyncio
async def test_startup_never_retries_interrupted_publish_automatically():
    collection=PublishCollection()
    assert await recover_interrupted_publishes(collection)==2
    query, update=collection.calls[-1]
    assert query=={"status":"Publishing"}
    assert update["$set"]["status"]=="Publish Review Required"


def test_publishing_commit_rejects_empty_message_list():
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(commit_published(PublishCollection(), 5, "j", [], datetime.now(timezone.utc)))


def test_legacy_v2_encrypted_session_remains_decryptable_for_upgrade():
    cipher=SessionCipher(Fernet.generate_key().decode())
    legacy=cipher.encrypt("legacy-telethon-session")
    assert cipher.decrypt_for_user(404,legacy)=="legacy-telethon-session"


def test_plural_material_names_are_not_mistaken_for_subjects():
    groups, _ = automatic_index([
        MessageRecord(1, "Radiology Planners"),
        MessageRecord(2, "Anatomy PDFs"),
        MessageRecord(3, "Biology Mock Tests"),
    ])
    assert groups["Radiology"]["planners"] == 1
    assert groups["Anatomy"]["pdfs"] == 2
    assert groups["Biology"]["tests"] == 3


from app.publishing import clear_chunk_inflight


@pytest.mark.asyncio
async def test_explicit_rate_limit_can_clear_inflight_before_safe_retry():
    collection=PublishCollection()
    await clear_chunk_inflight(collection, 8, "job")
    assert collection.calls[-1][1]["$set"]["publish_inflight_index"] is None


@pytest.mark.asyncio
async def test_job_store_accepts_waiting_for_resolution_terminal_scan_state():
    collection=FakeCollection(); store=JobStore(collection)
    await store.finish(9,"job","Waiting for Resolution",unresolved=2)
    assert collection.calls[-1][2]["$set"]["status"]=="Waiting for Resolution"
    assert collection.calls[-1][2]["$unset"]["active_key"]==""
