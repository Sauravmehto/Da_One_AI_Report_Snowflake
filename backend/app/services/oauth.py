from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.core.config import Settings


@dataclass
class OAuthTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    username: str | None = None


class OAuthService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def build_authorize_url(self, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self._settings.OAUTH_CLIENT_ID,
            "redirect_uri": self._settings.OAUTH_REDIRECT_URI,
            "state": state,
        }
        return f"{self._settings.oauth_authorize_url}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> OAuthTokens:
        return await self._request_tokens(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.OAUTH_REDIRECT_URI,
            }
        )

    async def refresh_tokens(self, refresh_token: str) -> OAuthTokens:
        return await self._request_tokens(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )

    async def _request_tokens(self, data: dict[str, str]) -> OAuthTokens:
        payload = {
            **data,
            "client_id": self._settings.OAUTH_CLIENT_ID,
            "client_secret": self._settings.OAUTH_CLIENT_SECRET,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self._settings.oauth_token_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            body = response.json()

        expires_in = int(body.get("expires_in", 3600))
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token", ""),
            expires_in=expires_in,
            username=body.get("username"),
        )

    @staticmethod
    def token_expires_at(expires_in: int) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=expires_in)
