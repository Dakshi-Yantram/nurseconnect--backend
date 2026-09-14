"""Async Redis client."""
import redis.asyncio as aioredis

from app.core.config import settings

redis_client: aioredis.Redis = aioredis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=3,   # fail fast if Redis is unreachable
    socket_timeout=3,           # fail fast if a call hangs mid-request
    retry_on_timeout=False,
)


async def get_redis() -> aioredis.Redis:
    return redis_client