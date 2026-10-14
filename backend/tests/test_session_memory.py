import pytest
from cryptography.fernet import Fernet

from app.session.memory import InMemorySessionStore
from app.session.base import SessionData
from datetime import UTC, datetime, timedelta


@pytest.fixture
def session_store() -> InMemorySessionStore:
    return InMemorySessionStore()


@pytest.fixture
def sample_session() -> SessionData:
    return SessionData(
        session_id="sess_test_123",
        snowflake_user="ALICE",
        role="ANALYST",
        access_token="access-token",
        refresh_token_encrypted="encrypted-refresh",
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.mark.asyncio
async def test_session_set_and_get(
    session_store: InMemorySessionStore,
    sample_session: SessionData,
) -> None:
    await session_store.set(sample_session.session_id, sample_session, 3600)
    loaded = await session_store.get(sample_session.session_id)

    assert loaded is not None
    assert loaded.snowflake_user == "ALICE"
    assert loaded.access_token == "access-token"


@pytest.mark.asyncio
async def test_session_delete(
    session_store: InMemorySessionStore,
    sample_session: SessionData,
) -> None:
    await session_store.set(sample_session.session_id, sample_session, 3600)
    await session_store.delete(sample_session.session_id)

    assert await session_store.get(sample_session.session_id) is None


@pytest.mark.asyncio
async def test_oauth_state_consume_once(session_store: InMemorySessionStore) -> None:
    await session_store.set_oauth_state("state-abc", 300)

    assert await session_store.consume_oauth_state("state-abc") is True
    assert await session_store.consume_oauth_state("state-abc") is False


@pytest.mark.asyncio
async def test_session_expires(
    session_store: InMemorySessionStore,
    sample_session: SessionData,
) -> None:
    await session_store.set(sample_session.session_id, sample_session, 0)
    assert await session_store.get(sample_session.session_id) is None
