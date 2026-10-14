from pathlib import Path

from app.core.config import Settings
from app.conversation.base import (
    Conversation,
    ConversationKind,
    ConversationStore,
    ConversationSummary,
    StoredMessage,
)
from app.conversation.file import FileConversationStore
from app.conversation.memory import InMemoryConversationStore
from app.conversation.redis import RedisConversationStore
from app.conversation.truncate import truncate_tool_result


def get_conversation_store(settings: Settings) -> ConversationStore:
    """Redis in production; on disk for local development.

    Conversations deliberately do NOT follow SESSION_BACKEND all the way down:
    losing a session on restart costs one click to sign back in, but losing
    chat history costs the work itself. So the memory backend gets a file store
    rather than an in-process one, and history survives the constant restarts
    of a dev loop. CONVERSATION_STORE=memory opts out (tests use it).
    """
    if settings.SESSION_BACKEND == "redis":
        return RedisConversationStore(settings.REDIS_URL)
    if settings.CONVERSATION_STORE == "memory":
        return InMemoryConversationStore()
    return FileConversationStore(Path(settings.CONVERSATION_DIR))


__all__ = [
    "Conversation",
    "ConversationKind",
    "ConversationStore",
    "ConversationSummary",
    "StoredMessage",
    "get_conversation_store",
    "truncate_tool_result",
]
