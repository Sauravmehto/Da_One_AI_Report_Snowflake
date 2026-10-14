import redis.asyncio as redis

from app.conversation.base import Conversation, ConversationSummary

CONVERSATION_PREFIX = "conversation:"
OWNER_INDEX_PREFIX = "conversations:owner:"


class RedisConversationStore:
    """Redis-backed conversations.

    Two keys per conversation:
      - `conversation:{id}`         the document, with a TTL
      - `conversations:owner:{who}` a sorted set of that owner's conversation
                                    ids, scored by updated_at

    The index is a ZSET rather than a plain SET so listing comes back
    newest-first without loading every document to sort. Because a document
    can expire while its id is still in the index, listing prunes ids whose
    document has gone — the index is a hint, the documents are the truth.
    """

    def __init__(self, redis_url: str, client=None) -> None:
        # `client` is an injection point for tests (e.g. fakeredis), so the
        # Redis path can be covered without a live server.
        self._redis = client if client is not None else redis.from_url(
            redis_url, decode_responses=True
        )

    async def get(self, conversation_id: str) -> Conversation | None:
        raw = await self._redis.get(f"{CONVERSATION_PREFIX}{conversation_id}")
        if raw is None:
            return None
        return Conversation.model_validate_json(raw)

    async def save(self, conversation: Conversation, ttl_seconds: int) -> None:
        await self._redis.set(
            f"{CONVERSATION_PREFIX}{conversation.conversation_id}",
            conversation.model_dump_json(),
            ex=ttl_seconds,
        )
        index_key = f"{OWNER_INDEX_PREFIX}{conversation.owner}"
        await self._redis.zadd(
            index_key,
            {conversation.conversation_id: conversation.updated_at.timestamp()},
        )
        # Keep the index alive at least as long as any conversation in it.
        await self._redis.expire(index_key, ttl_seconds)

    async def list_for_owner(self, owner: str) -> list[ConversationSummary]:
        index_key = f"{OWNER_INDEX_PREFIX}{owner}"
        ids = await self._redis.zrevrange(index_key, 0, -1)
        if not ids:
            return []

        docs = await self._redis.mget(
            [f"{CONVERSATION_PREFIX}{cid}" for cid in ids]
        )

        summaries: list[ConversationSummary] = []
        stale: list[str] = []
        for cid, raw in zip(ids, docs, strict=True):
            if raw is None:
                stale.append(cid)
                continue
            c = Conversation.model_validate_json(raw)
            summaries.append(
                ConversationSummary(
                    conversation_id=c.conversation_id,
                    title=c.title,
                    database=c.database,
                    dashboard_id=c.dashboard_id,
                    kind=c.kind,
                    pinned=c.pinned,
                    message_count=len(c.messages),
                    created_at=c.created_at,
                    updated_at=c.updated_at,
                )
            )

        if stale:
            await self._redis.zrem(index_key, *stale)

        return summaries

    async def delete(self, conversation_id: str) -> None:
        # Read first so the owner index can be cleaned up too; a conversation
        # that is already gone just leaves a stale id, which listing prunes.
        conversation = await self.get(conversation_id)
        await self._redis.delete(f"{CONVERSATION_PREFIX}{conversation_id}")
        if conversation is not None:
            await self._redis.zrem(
                f"{OWNER_INDEX_PREFIX}{conversation.owner}", conversation_id
            )

    async def close(self) -> None:
        await self._redis.aclose()
