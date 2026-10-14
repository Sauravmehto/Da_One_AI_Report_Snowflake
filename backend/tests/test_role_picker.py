"""Role picker allowlist (SNOWFLAKE_AVAILABLE_ROLES) and /auth/role switch."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("SESSION_BACKEND", "memory")
os.environ.setdefault("SESSION_STORE", "memory")
os.environ.setdefault("CONVERSATION_STORE", "memory")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("OAUTH_CLIENT_ID", "test-client")
os.environ.setdefault("OAUTH_CLIENT_SECRET", "test-secret")
os.environ.setdefault("SNOWFLAKE_ACCOUNT", "test-account")
os.environ.setdefault("SNOWFLAKE_WAREHOUSE", "TEST_WH")
if not os.environ.get("TOKEN_ENCRYPTION_KEY"):
    os.environ["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app.core.config import Settings
from app.main import app
from app.session.base import SessionData


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


async def _seed_session(session_id: str = "role-session", role: str | None = "ANALYST") -> None:
    session = SessionData(
        session_id=session_id,
        snowflake_user="ALICE",
        role=role,
        access_token="token",
        refresh_token_encrypted="enc",
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await app.state.session_store.set(session_id, session, 3600)


def test_available_roles_parses_comma_list() -> None:
    settings = Settings(
        SNOWFLAKE_ACCOUNT="a",
        SNOWFLAKE_AVAILABLE_ROLES="role_a, Role_B,ROLE_A",
    )
    assert settings.available_roles() == ["ROLE_A", "ROLE_B"]
    assert settings.is_available_role("role_b") is True
    assert settings.is_available_role("OTHER") is False


def test_available_roles_empty_by_default() -> None:
    settings = Settings(SNOWFLAKE_ACCOUNT="a", SNOWFLAKE_AVAILABLE_ROLES="")
    assert settings.available_roles() == []


@pytest.mark.asyncio
async def test_list_roles_empty_allowlist(client: TestClient) -> None:
    await _seed_session()
    client.cookies.set("session_id", "role-session")

    with patch("app.api.v1.routes.auth.settings") as mock_settings:
        mock_settings.available_roles.return_value = []
        response = client.get("/api/v1/auth/roles")

    assert response.status_code == 200
    assert response.json() == {"roles": [], "current": "ANALYST"}


@pytest.mark.asyncio
async def test_list_roles_returns_allowlist(client: TestClient) -> None:
    await _seed_session()
    client.cookies.set("session_id", "role-session")

    with patch("app.api.v1.routes.auth.settings") as mock_settings:
        mock_settings.available_roles.return_value = ["PRIVATE_CREDIT_ANALYST", "DATAALPHA_ADMIN"]
        response = client.get("/api/v1/auth/roles")

    assert response.status_code == 200
    body = response.json()
    assert body["roles"] == ["PRIVATE_CREDIT_ANALYST", "DATAALPHA_ADMIN"]
    assert body["current"] == "ANALYST"


@pytest.mark.asyncio
async def test_set_role_rejects_outside_allowlist(client: TestClient) -> None:
    await _seed_session()
    client.cookies.set("session_id", "role-session")

    with patch("app.api.v1.routes.auth.settings") as mock_settings:
        mock_settings.is_available_role.return_value = False
        mock_settings.available_roles.return_value = ["ALLOWED_ROLE"]
        response = client.post("/api/v1/auth/role", json={"role": "NOT_ALLOWED"})

    assert response.status_code == 400
    assert "SNOWFLAKE_AVAILABLE_ROLES" in response.json()["detail"]


@pytest.mark.asyncio
async def test_set_role_rejects_empty_allowlist_post(client: TestClient) -> None:
    await _seed_session()
    client.cookies.set("session_id", "role-session")

    with patch("app.api.v1.routes.auth.settings") as mock_settings:
        mock_settings.is_available_role.return_value = False
        response = client.post("/api/v1/auth/role", json={"role": "ANY_ROLE"})

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_set_role_success_updates_session(client: TestClient) -> None:
    await _seed_session(role="OLD_ROLE")
    client.cookies.set("session_id", "role-session")

    with (
        patch("app.api.v1.routes.auth.settings") as mock_settings,
        patch("app.api.v1.routes.auth._get_snowflake_service") as mock_sf_factory,
    ):
        mock_settings.is_available_role.return_value = True
        mock_settings.is_admin_role.return_value = False
        mock_settings.SESSION_TTL_SECONDS = 3600
        snowflake = AsyncMock()
        snowflake.verify_role.return_value = "NEW_ROLE"
        mock_sf_factory.return_value = snowflake

        response = client.post("/api/v1/auth/role", json={"role": "new_role"})

    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "NEW_ROLE"
    assert body["snowflake_user"] == "ALICE"

    stored = await app.state.session_store.get("role-session")
    assert stored is not None
    assert stored.role == "NEW_ROLE"
    snowflake.verify_role.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_role_snowflake_refusal(client: TestClient) -> None:
    await _seed_session()
    client.cookies.set("session_id", "role-session")

    with (
        patch("app.api.v1.routes.auth.settings") as mock_settings,
        patch("app.api.v1.routes.auth._get_snowflake_service") as mock_sf_factory,
    ):
        mock_settings.is_available_role.return_value = True
        snowflake = AsyncMock()
        snowflake.verify_role.side_effect = RuntimeError("not granted")
        mock_sf_factory.return_value = snowflake

        response = client.post("/api/v1/auth/role", json={"role": "ALLOWED_ROLE"})

    assert response.status_code == 400
    assert "refused" in response.json()["detail"].lower()
