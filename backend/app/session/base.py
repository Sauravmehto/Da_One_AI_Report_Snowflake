from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel


class SessionData(BaseModel):
    session_id: str
    snowflake_user: str
    role: str | None = None
    access_token: str
    refresh_token_encrypted: str
    token_expires_at: datetime
    # How this session authenticates to Snowflake. "oauth" is the real path
    # (per-user token, refreshed on expiry). The rest are TEMPORARY dev-login
    # paths with no token to refresh: "keypair" is the shared service account,
    # while "externalbrowser" and "password" authenticate as the developer's
    # own Snowflake user. Defaults to "oauth" so existing sessions are
    # unaffected.
    auth_mode: Literal["oauth", "keypair", "externalbrowser", "password"] = "oauth"


class SessionStore(Protocol):
    async def get(self, session_id: str) -> SessionData | None: ...

    async def set(self, session_id: str, data: SessionData, ttl_seconds: int) -> None: ...

    async def delete(self, session_id: str) -> None: ...

    async def set_oauth_state(self, state: str, ttl_seconds: int) -> None: ...

    async def consume_oauth_state(self, state: str) -> bool: ...

    async def close(self) -> None: ...
