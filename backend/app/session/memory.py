import asyncio
from datetime import UTC, datetime, timedelta

from app.session.base import SessionData


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, tuple[SessionData, datetime]] = {}
        self._oauth_states: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    def _purge_expired(self) -> None:
        now = datetime.now(UTC)
        expired_sessions = [
            key for key, (_, expires_at) in self._sessions.items() if expires_at <= now
        ]
        for key in expired_sessions:
            del self._sessions[key]

        expired_states = [
            key for key, expires_at in self._oauth_states.items() if expires_at <= now
        ]
        for key in expired_states:
            del self._oauth_states[key]

    async def get(self, session_id: str) -> SessionData | None:
        async with self._lock:
            self._purge_expired()
            entry = self._sessions.get(session_id)
            if entry is None:
                return None
            data, expires_at = entry
            if expires_at <= datetime.now(UTC):
                del self._sessions[session_id]
                return None
            return data

    async def set(self, session_id: str, data: SessionData, ttl_seconds: int) -> None:
        async with self._lock:
            self._purge_expired()
            self._sessions[session_id] = (
                data,
                datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            )

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def set_oauth_state(self, state: str, ttl_seconds: int) -> None:
        async with self._lock:
            self._purge_expired()
            self._oauth_states[state] = datetime.now(UTC) + timedelta(
                seconds=ttl_seconds
            )

    async def consume_oauth_state(self, state: str) -> bool:
        async with self._lock:
            self._purge_expired()
            expires_at = self._oauth_states.pop(state, None)
            if expires_at is None:
                return False
            return expires_at > datetime.now(UTC)

    async def close(self) -> None:
        return None
