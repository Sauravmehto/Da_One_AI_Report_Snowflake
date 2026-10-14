import asyncio
from datetime import UTC, datetime, timedelta

from app.conversation.base import Conversation, ConversationSummary


class InMemoryConversationStore:
    """Local-dev conversation store. Conversations do not survive a restart —
    use the Redis store for anything that must persist."""

    def __init__(self) -> None:
        self._conversations: dict[str, tuple[Conversation, datetime]] = {}
        self._lock = asyncio.Lock()

    def _purge_expired(self) -> None:
        now = datetime.now(UTC)
        expired = [
            key for key, (_, expires_at) in self._conversations.items()
            if expires_at <= now
        ]
        for key in expired:
            del self._conversations[key]

    async def get(self, conversation_id: str) -> Conversation | None:
        async with self._lock:
            self._purge_expired()
            entry = self._conversations.get(conversation_id)
            if entry is None:
                return None
            conversation, expires_at = entry
            if expires_at <= datetime.now(UTC):
                del self._conversations[conversation_id]
                return None
            # Return a copy: the caller mutates (appends turns) and must not
            # write through to stored state without an explicit save().
            return conversation.model_copy(deep=True)

    async def save(self, conversation: Conversation, ttl_seconds: int) -> None:
        async with self._lock:
            self._purge_expired()
            self._conversations[conversation.conversation_id] = (
                conversation.model_copy(deep=True),
                datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            )

    async def list_for_owner(self, owner: str) -> list[ConversationSummary]:
        async with self._lock:
            self._purge_expired()
            summaries = [
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
                for c, _ in self._conversations.values()
                if c.owner == owner
            ]
        summaries.sort(key=lambda s: (not s.pinned, s.updated_at), reverse=True)
        return summaries

    async def delete(self, conversation_id: str) -> None:
        async with self._lock:
            self._conversations.pop(conversation_id, None)

    async def close(self) -> None:
        return None
