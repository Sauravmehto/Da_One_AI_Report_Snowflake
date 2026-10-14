"""File-backed conversation store — the local-development default.

Why this exists: the in-memory store loses every conversation when the backend
restarts, and during development the backend restarts constantly. Redis solves
it properly but means running a server, which is a lot to ask of a laptop just
to keep chat history across an edit-reload cycle.

One JSON file per conversation under `.conversations/`, so a corrupted or
half-written file costs exactly that conversation rather than the whole
history. Not built for concurrency or scale — use Redis for anything
multi-process. This is a development convenience with an honest ceiling.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.conversation.base import Conversation, ConversationSummary

log = logging.getLogger(__name__)


class FileConversationStore:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _path(self, conversation_id: str) -> Path:
        # Ids are URL-safe tokens we generate, but never trust one to be a safe
        # filename — a traversal here would write outside the store.
        safe = "".join(c for c in conversation_id if c.isalnum() or c in "-_")
        return self._dir / f"{safe}.json"

    def _read(self, path: Path) -> Conversation | None:
        try:
            return Conversation.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a bad file loses one conversation, not all
            log.warning("Skipping unreadable conversation file: %s", path.name)
            return None

    def _is_expired(self, conversation: Conversation, ttl_seconds: int) -> bool:
        return conversation.updated_at + timedelta(seconds=ttl_seconds) <= datetime.now(UTC)

    async def get(self, conversation_id: str) -> Conversation | None:
        async with self._lock:
            path = self._path(conversation_id)
            if not path.is_file():
                return None
            return self._read(path)

    async def save(self, conversation: Conversation, ttl_seconds: int) -> None:
        async with self._lock:
            path = self._path(conversation.conversation_id)
            # Write to a temp file then replace, so an interrupted save cannot
            # leave a half-written JSON file behind.
            tmp = path.with_suffix(".tmp")
            tmp.write_text(conversation.model_dump_json(), encoding="utf-8")
            tmp.replace(path)

    async def list_for_owner(self, owner: str) -> list[ConversationSummary]:
        async with self._lock:
            summaries: list[ConversationSummary] = []
            for path in self._dir.glob("*.json"):
                conversation = self._read(path)
                if conversation is None or conversation.owner != owner:
                    continue
                summaries.append(
                    ConversationSummary(
                        conversation_id=conversation.conversation_id,
                        title=conversation.title,
                        database=conversation.database,
                        dashboard_id=conversation.dashboard_id,
                        kind=conversation.kind,
                        pinned=conversation.pinned,
                        message_count=len(conversation.messages),
                        created_at=conversation.created_at,
                        updated_at=conversation.updated_at,
                    )
                )
        summaries.sort(key=lambda s: (not s.pinned, s.updated_at), reverse=True)
        return summaries

    async def delete(self, conversation_id: str) -> None:
        async with self._lock:
            self._path(conversation_id).unlink(missing_ok=True)

    async def close(self) -> None:
        return None
