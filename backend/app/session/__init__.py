from pathlib import Path

from app.core.config import Settings
from app.session.base import SessionData, SessionStore
from app.session.file import FileSessionStore
from app.session.memory import InMemorySessionStore
from app.session.redis import RedisSessionStore


def get_session_store(settings: Settings) -> SessionStore:
    """Redis in production; on disk for local development.

    The file store keeps a dev-login session across backend restarts, which
    otherwise sign you out constantly. It never writes an OAuth session to
    disk — see app/session/file.py. SESSION_STORE=memory opts out (tests).
    """
    if settings.SESSION_BACKEND == "redis":
        return RedisSessionStore(settings.REDIS_URL)
    if settings.SESSION_STORE == "memory":
        return InMemorySessionStore()
    return FileSessionStore(Path(settings.SESSION_DIR))


__all__ = ["SessionData", "SessionStore", "get_session_store"]
