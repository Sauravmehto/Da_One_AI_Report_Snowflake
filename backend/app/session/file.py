"""File-backed session store — local development only.

Sessions were in-process, so every backend restart signed you out. During a dev
loop that means constant 401s and a dead-looking page.

**OAuth sessions are never written to disk.** They carry a live Snowflake
access token, and a plaintext token in a file is a materially worse trade than
signing in again. Those stay in memory and are lost on restart, exactly as
before. Only key-pair dev-login sessions persist — their `access_token` is
empty by construction, so nothing sensitive is written.

Production uses Redis and never reaches this.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.session.base import SessionData

log = logging.getLogger(__name__)


class FileSessionStore:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        # OAuth sessions and short-lived OAuth state live here, never on disk.
        self._volatile: dict[str, tuple[SessionData, datetime]] = {}
        self._oauth_states: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    def _path(self, session_id: str) -> Path:
        # Never trust an id as a filename, even one we generated.
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
        return self._dir / f"{safe}.json"

    async def get(self, session_id: str) -> SessionData | None:
        async with self._lock:
            entry = self._volatile.get(session_id)
            if entry is not None:
                data, expires_at = entry
                if expires_at <= datetime.now(UTC):
                    del self._volatile[session_id]
                    return None
                return data

            path = self._path(session_id)
            if not path.is_file():
                return None
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                expires_at = datetime.fromisoformat(payload["expires_at"])
                if expires_at <= datetime.now(UTC):
                    path.unlink(missing_ok=True)
                    return None
                return SessionData.model_validate(payload["session"])
            except Exception:  # noqa: BLE001 - a bad file costs one session
                log.warning("Discarding unreadable session file: %s", path.name)
                path.unlink(missing_ok=True)
                return None

    async def set(self, session_id: str, data: SessionData, ttl_seconds: int) -> None:
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)

        # A live OAuth token must not be written to disk.
        if data.auth_mode != "keypair":
            async with self._lock:
                self._volatile[session_id] = (data, expires_at)
            return

        async with self._lock:
            path = self._path(session_id)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "expires_at": expires_at.isoformat(),
                        "session": json.loads(data.model_dump_json()),
                    }
                ),
                encoding="utf-8",
            )
            tmp.replace(path)

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._volatile.pop(session_id, None)
            self._path(session_id).unlink(missing_ok=True)

    async def set_oauth_state(self, state: str, ttl_seconds: int) -> None:
        # Deliberately in memory: this is a seconds-lived CSRF nonce, and it is
        # consumed by the same process that issued it.
        async with self._lock:
            self._oauth_states[state] = datetime.now(UTC) + timedelta(seconds=ttl_seconds)

    async def consume_oauth_state(self, state: str) -> bool:
        async with self._lock:
            expires_at = self._oauth_states.pop(state, None)
            return expires_at is not None and expires_at > datetime.now(UTC)

    async def close(self) -> None:
        return None
