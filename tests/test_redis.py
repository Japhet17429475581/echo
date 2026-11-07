import asyncio
import os
import uuid

import pytest
from redis.asyncio import Redis

from echo.ai import RESERVE, SETTLE, UNLOCK


@pytest.mark.skipif(not os.getenv("ECHO_TEST_REDIS_URL"), reason="real Redis contract")
async def test_real_redis_atomic_budget_and_token_lock():
    redis = Redis.from_url(os.environ["ECHO_TEST_REDIS_URL"], decode_responses=True)
    prefix = "echo:test:" + uuid.uuid4().hex
    keys = [prefix + ":global", prefix + ":user", prefix + ":operation"]
    lock = prefix + ":lock"
    try:
        results = await asyncio.gather(
            *[redis.eval(RESERVE, 3, *keys, 4, 400, 86400, 8, 8, 8, 800) for _ in range(10)]
        )
        assert sum(results) == 2
        assert int(await redis.hget(keys[0], "requests")) == 8
        await redis.eval(SETTLE, 3, *keys, 3, 300)
        assert int(await redis.hget(keys[0], "requests")) == 5
        assert int(await redis.hget(keys[0], "micros")) == 500
        await redis.set(lock, "owner", ex=5)
        assert await redis.eval(UNLOCK, 1, lock, "stranger") == 0
        assert await redis.get(lock) == "owner"
        assert await redis.eval(UNLOCK, 1, lock, "owner") == 1
    finally:
        await redis.delete(*keys, lock)
        await redis.aclose()
