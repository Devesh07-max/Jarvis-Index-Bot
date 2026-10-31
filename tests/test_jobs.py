import pytest
from app.jobs import JobStore

class Result:
    modified_count = 2

class FakeCollection:
    def __init__(self): self.calls=[]
    async def create_index(self, *args, **kwargs): self.calls.append(("index", args, kwargs))
    async def insert_one(self, doc): self.doc=doc
    async def update_many(self, query, update): self.calls.append(("many", query, update)); return Result()
    async def find_one_and_update(self, query, update, **kwargs): self.calls.append(("claim", query, update, kwargs)); return {"job_id":"j"}
    async def update_one(self, query, update): self.calls.append(("one", query, update)); return Result()

@pytest.mark.asyncio
async def test_job_store_persists_checkpoint_and_resumes_state():
    collection=FakeCollection(); store=JobStore(collection)
    await store.enqueue(user_id=7, job_id="j", channel_id=-1001, channel_title="Course", mode="manual", template="x", chat_id=9)
    assert collection.doc["status"] == "Queued"
    assert collection.doc["checkpoint_last_id"] == 0
    assert collection.doc["active_key"] == "7:-1001"
    assert await store.recover_interrupted() == 2
    assert (await store.claim("j"))["job_id"] == "j"
    await store.checkpoint(7,"j",123,456)
    query=collection.calls[-1][1]
    assert query["user_id"] == 7 and query["job_id"] == "j"
    assert await store.request_cancel(7,"j")
    with pytest.raises(ValueError): await store.finish(7,"j","Published")
