"""Which role a session's Snowflake connection actually runs under.

This is the mechanism behind per-user access control: the databases a user can
see, and the rows within them, are decided by the role on their connection.
Getting it wrong does not raise anything — it hands everyone the same access
while the app still looks like it is enforcing per-user permissions — so the
behaviour is pinned here rather than left to inspection.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.services.snowflake import SnowflakeService
from app.session.base import SessionData


def _session(**overrides) -> SessionData:
    base = {
        "session_id": "s1",
        "snowflake_user": "ALICE",
        "access_token": "token-123",
        "refresh_token_encrypted": "",
        "token_expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    return SessionData(**{**base, **overrides})


@pytest.fixture
def captured_connect(monkeypatch):
    """Capture the kwargs handed to snowflake.connector.connect."""
    calls: list[dict] = []

    def fake_connect(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(
        "snowflake.connector.connect", fake_connect
    )
    return calls


def _service(**setting_overrides) -> SnowflakeService:
    settings = Settings(
        SNOWFLAKE_ACCOUNT="test-account",
        SNOWFLAKE_WAREHOUSE="TEST_WH",
        **setting_overrides,
    )
    return SnowflakeService(settings, session_store=None, encryptor=None)  # type: ignore[arg-type]


def test_oauth_session_without_a_role_sends_no_role(captured_connect):
    """The important one.

    No role on the connection means Snowflake applies the USER's own default
    role. Sending the configured role instead would flatten every user onto it,
    and two people with completely different grants would get identical access.
    """
    service = _service(SNOWFLAKE_DEFAULT_ROLE="READ_WRITE_LOAN_DB_ROLE")
    service._connect(_session(role=None))

    assert "role" not in captured_connect[0]


def test_oauth_session_uses_its_own_resolved_role(captured_connect):
    service = _service(SNOWFLAKE_DEFAULT_ROLE="READ_WRITE_LOAN_DB_ROLE")
    service._connect(_session(role="PRIVATE_CREDIT_ANALYST"))

    assert captured_connect[0]["role"] == "PRIVATE_CREDIT_ANALYST"


def test_keypair_session_falls_back_to_the_configured_role(
    captured_connect, monkeypatch
):
    """The service account has no user to inherit a role from, so the
    configured default is the only thing it can use — and is still correct."""
    service = _service(SNOWFLAKE_DEFAULT_ROLE="READ_WRITE_LOAN_DB_ROLE")
    monkeypatch.setattr(
        SnowflakeService, "_load_private_key", lambda self: b"key"
    )
    service._connect(_session(auth_mode="keypair", role=None))

    assert captured_connect[0]["role"] == "READ_WRITE_LOAN_DB_ROLE"
