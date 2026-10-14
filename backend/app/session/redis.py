from datetime import UTC, datetime, timedelta

import redis.asyncio as redis

from app.session.base import SessionData

SESSION_PREFIX = "session:"
OAUTH_STATE_PREFIX = "oauth_state:"


class RedisSessionStore:
    def __init__(self, redis_url: str) -> None:
        self._redis = redis.from_url(redis_url, decode_responses=True)

    async def get(self, session_id: str) -> SessionData | None:
        raw = await self._redis.get(f"{SESSION_PREFIX}{session_id}")
        if raw is None:
            return None
        return SessionData.model_validate_json(raw)

    async def set(self, session_id: str, data: SessionData, ttl_seconds: int) -> None:
        await self._redis.setex(
            f"{SESSION_PREFIX}{session_id}",
            ttl_seconds,
            data.model_dump_json(),
        )

    async def delete(self, session_id: str) -> None:
        await self._redis.delete(f"{SESSION_PREFIX}{session_id}")

    async def set_oauth_state(self, state: str, ttl_seconds: int) -> None:
        await self._redis.setex(f"{OAUTH_STATE_PREFIX}{state}", ttl_seconds, "1")

    async def consume_oauth_state(self, state: str) -> bool:
        key = f"{OAUTH_STATE_PREFIX}{state}"
        deleted = await self._redis.delete(key)
        return deleted == 1

    async def close(self) -> None:
        await self._redis.aclose()
