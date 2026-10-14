"""Conversation store: both implementations behave identically.

The Redis implementation is exercised against fakeredis rather than being
left untested until deployment — an unrun store is an unknown store.
"""

import fakeredis.aioredis
import pytest

from app.conversation.base import Conversation
from app.conversation.file import FileConversationStore
from app.conversation.memory import InMemoryConversationStore
from app.conversation.redis import RedisConversationStore

TTL = 3600


def _conversation(cid: str = "c1", owner: str = "SVC_USER") -> Conversation:
    return Conversation(conversation_id=cid, owner=owner, database="PRIVATECREDIT")


@pytest.fixture(params=["memory", "redis", "file"])
def store(request, tmp_path):
    """Every test runs against ALL implementations, so they cannot drift."""
    if request.param == "memory":
        return InMemoryConversationStore()
    if request.param == "file":
        return FileConversationStore(tmp_path / "conversations")
    return RedisConversationStore("", client=fakeredis.aioredis.FakeRedis(decode_responses=True))


async def test_roundtrip_preserves_messages(store):
    c = _conversation()
    c.append("user", [{"type": "text", "text": "how many deals?"}])
    c.append("assistant", [{"type": "text", "text": "20 deals."}])
    await store.save(c, TTL)

    loaded = await store.get("c1")
    assert loaded is not None
    assert [m.role for m in loaded.messages] == ["user", "assistant"]
    assert loaded.messages[0].content[0]["text"] == "how many deals?"
    assert loaded.database == "PRIVATECREDIT"


async def test_tool_use_blocks_survive_verbatim(store):
    """Tool blocks must round-trip unchanged — the agent loop hands them
    straight back to the model, so any lossy storage breaks the next turn."""
    c = _conversation()
    c.append("assistant", [
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": "tu_1", "name": "run_safe_sql",
         "input": {"sql": "SELECT 1"}},
    ])
    c.append("user", [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "{\"rows\": [[1]]}"},
    ])
    await store.save(c, TTL)

    loaded = await store.get("c1")
    assert loaded.messages[0].content[1]["name"] == "run_safe_sql"
    assert loaded.messages[0].content[1]["input"] == {"sql": "SELECT 1"}
    assert loaded.messages[1].content[0]["tool_use_id"] == "tu_1"


async def test_to_model_messages_shape(store):
    c = _conversation()
    c.append("user", [{"type": "text", "text": "hi"}])
    await store.save(c, TTL)

    loaded = await store.get("c1")
    assert loaded.to_model_messages() == [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    ]


async def test_missing_conversation_returns_none(store):
    assert await store.get("does-not-exist") is None


async def test_list_is_scoped_to_owner_and_newest_first(store):
    import asyncio

    a = _conversation("a", owner="ALICE")
    a.append("user", [{"type": "text", "text": "first"}])
    await store.save(a, TTL)

    await asyncio.sleep(0.01)  # keep updated_at strictly ordered

    b = _conversation("b", owner="ALICE")
    b.append("user", [{"type": "text", "text": "second"}])
    await store.save(b, TTL)

    other = _conversation("c", owner="BOB")
    await store.save(other, TTL)

    listed = await store.list_for_owner("ALICE")
    assert [s.conversation_id for s in listed] == ["b", "a"]
    assert listed[0].message_count == 1
    # BOB's conversation must not leak into ALICE's list.
    assert all(s.conversation_id != "c" for s in listed)


async def test_delete_removes_from_get_and_list(store):
    c = _conversation()
    await store.save(c, TTL)
    await store.delete("c1")

    assert await store.get("c1") is None
    assert await store.list_for_owner("SVC_USER") == []


async def test_mutating_a_loaded_copy_does_not_write_through(store):
    """get() must hand back a copy: appending to it should not change stored
    state until save() is called explicitly."""
    c = _conversation()
    c.append("user", [{"type": "text", "text": "one"}])
    await store.save(c, TTL)

    loaded = await store.get("c1")
    loaded.append("user", [{"type": "text", "text": "two — not saved"}])

    reloaded = await store.get("c1")
    assert len(reloaded.messages) == 1
