import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ["SESSION_BACKEND"] = "memory"
os.environ["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["JWT_SECRET"] = "test-secret"
os.environ["OAUTH_CLIENT_ID"] = "test-client"
os.environ["OAUTH_CLIENT_SECRET"] = "test-secret"
os.environ["SNOWFLAKE_ACCOUNT"] = "test-account"
os.environ["SNOWFLAKE_WAREHOUSE"] = "TEST_WH"

from app.api.v1.routes import auth as auth_routes
from app.main import app
from app.services.oauth import OAuthTokens
from app.session.base import SessionData


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_me_unauthenticated(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401


def test_login_redirects_to_snowflake(client: TestClient) -> None:
    response = client.get("/api/v1/auth/login", follow_redirects=False)
    assert response.status_code == 302
    assert "snowflakecomputing.com/oauth/authorize" in response.headers["location"]


@patch("app.api.v1.routes.auth._get_oauth_service")
def test_callback_creates_session_cookie(
    mock_oauth_factory,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pinned rather than read from the developer's .env: FRONTEND_URL legitimately
    # differs per machine (whatever port the dev server is on), and asserting the
    # real setting made this test fail for a correct configuration.
    monkeypatch.setattr(auth_routes.settings, "FRONTEND_URL", "http://localhost:4321")

    oauth_service = AsyncMock()
    oauth_service.exchange_code.return_value = OAuthTokens(
        access_token="access-123",
        refresh_token="refresh-456",
        expires_in=3600,
        username="ALICE",
    )
    mock_oauth_factory.return_value = oauth_service

    state = "fixed-test-state"
    asyncio.run(app.state.session_store.set_oauth_state(state, 600))

    response = client.get(
        f"/api/v1/auth/callback?code=test-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "http://localhost:4321"
    assert "session_id" in response.cookies

    me_response = client.get("/api/v1/auth/me", cookies=response.cookies)
    assert me_response.status_code == 200
    assert me_response.json()["snowflake_user"] == "ALICE"


@pytest.mark.asyncio
async def test_logout_clears_session(client: TestClient) -> None:
    session = SessionData(
        session_id="logout-session",
        snowflake_user="BOB",
        access_token="token",
        refresh_token_encrypted="enc",
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    await app.state.session_store.set("logout-session", session, 3600)

    client.cookies.set("session_id", "logout-session")
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 204

    me_response = client.get("/api/v1/auth/me")
    assert me_response.status_code == 401
