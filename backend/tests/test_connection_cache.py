"""The per-session connection cache, and the two ways a connection leaves it.

The failure this guards: a token refresh closed the one connection a session's
requests share, so a dashboard build already running failed every remaining
query with "Connection is closed". Nothing announced it, and it recovered only
when a later refresh replaced the connection.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services import snowflake as snowflake_service
from app.services.connection_cache import ConnectionCache


class FakeConn:
    def __init__(self, name: str = "c") -> None:
        self.name = name
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _cache(grace: float = 60.0):
    clock = Clock()
    return ConnectionCache(grace_seconds=grace, clock=clock), clock


# --- retire: a token refresh -------------------------------------------------


def test_a_refresh_does_not_close_the_connection_a_request_is_using():
    cache, _ = _cache()
    in_use = cache.get_or_create("s1", FakeConn)

    cache.retire("s1")

    assert in_use.closed is False, "a running request lost its connection"


def test_the_next_request_after_a_refresh_gets_a_new_connection():
    cache, _ = _cache()
    old = cache.get_or_create("s1", lambda: FakeConn("old"))

    cache.retire("s1")
    new = cache.get_or_create("s1", lambda: FakeConn("new"))

    assert new is not old and new.name == "new"


def test_a_retired_connection_is_closed_once_its_grace_period_is_over():
    cache, clock = _cache(grace=60)
    old = cache.get_or_create("s1", FakeConn)
    cache.retire("s1")

    clock.now += 59
    cache.get_or_create("s2", FakeConn)
    assert old.closed is False

    clock.now += 2
    cache.get_or_create("s2", FakeConn)
    assert old.closed is True, "retired connections must not leak forever"


def test_repeated_refreshes_each_keep_their_own_grace_period():
    cache, clock = _cache(grace=60)
    first = cache.get_or_create("s1", FakeConn)
    cache.retire("s1")
    clock.now += 40
    second = cache.get_or_create("s1", FakeConn)
    cache.retire("s1")

    clock.now += 30  # first is 70s old, second 30s
    cache.get_or_create("s1", FakeConn)

    assert first.closed is True
    assert second.closed is False


# --- evict: logout or a role switch ------------------------------------------


def test_logout_and_role_switch_still_close_immediately():
    cache, _ = _cache()
    conn = cache.get_or_create("s1", FakeConn)

    cache.evict("s1")

    assert conn.closed is True


# --- a connection closed by anything else -----------------------------------


def test_a_closed_cached_connection_is_replaced_rather_than_handed_out():
    """The second half of the defect: the cache never checked, so a connection
    the server or network had closed kept failing every request."""
    cache, _ = _cache()
    dead = cache.get_or_create("s1", lambda: FakeConn("dead"))
    dead.closed = True

    got = cache.get_or_create("s1", lambda: FakeConn("fresh"))

    assert got is not dead and got.name == "fresh"


def test_a_connection_whose_check_raises_is_treated_as_closed():
    class Broken(FakeConn):
        def is_closed(self) -> bool:
            raise RuntimeError("socket gone")

    cache, _ = _cache()
    cache.get_or_create("s1", Broken)

    got = cache.get_or_create("s1", lambda: FakeConn("fresh"))

    assert got.name == "fresh"


def test_a_healthy_connection_is_reused():
    cache, _ = _cache()
    first = cache.get_or_create("s1", FakeConn)
    assert cache.get_or_create("s1", FakeConn) is first


# --- the service wires refresh to retire, not evict --------------------------


def test_a_token_refresh_retires_rather_than_evicts(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(snowflake_service.connection_cache, "retire",
                        lambda sid: calls.append(("retire", sid)))
    monkeypatch.setattr(snowflake_service.connection_cache, "evict",
                        lambda sid: calls.append(("evict", sid)))

    class Store:
        async def set(self, *a, **k):
            return None

    class Encryptor:
        def decrypt(self, value):
            return "refresh"

        def encrypt(self, value):
            return "enc"

    service = snowflake_service.SnowflakeService.__new__(snowflake_service.SnowflakeService)
    service._settings = SimpleNamespace(SESSION_TTL_SECONDS=60)
    service._session_store = Store()
    service._encryptor = Encryptor()

    async def refresh_tokens(token):
        return SimpleNamespace(access_token="new", refresh_token=None, expires_in=600)

    service._oauth = SimpleNamespace(refresh_tokens=refresh_tokens)

    session = SimpleNamespace(
        auth_mode="oauth", session_id="s1", refresh_token_encrypted="x",
        token_expires_at=datetime.now(UTC) + timedelta(seconds=30),
        model_copy=lambda update: SimpleNamespace(**update),
    )
    asyncio.run(service.ensure_fresh_session(session))

    assert calls == [("retire", "s1")]
