import asyncio
import contextlib
import time
import uuid
from contextlib import asynccontextmanager

from echo.ai import SLOT

RENEW = """
local score=redis.call('ZSCORE',KEYS[1],ARGV[1])
if not score or tonumber(score)<=tonumber(ARGV[3]) then return 0 end
redis.call('ZADD',KEYS[1],ARGV[2],ARGV[1]);return 1
"""


class ResourceLost(RuntimeError):
    pass


class HeavySlots:
    """Deployment-wide leases, shared by searches, tools and queued exports."""

    def __init__(self, redis, config):
        self.redis, self.config = redis, config
        self.key = "echo:resources:heavy"

    @asynccontextmanager
    async def hold(self):
        token = str(uuid.uuid4())
        ttl = self.config.lease_seconds
        while not await self.redis.eval(  # noqa: ASYNC110 -- distributed lease polling
            SLOT, 1, self.key, time.time(), time.time() + ttl, self.config.heavy_slots, token, ttl * 2
        ):
            await asyncio.sleep(0.25)
        parent = asyncio.current_task()

        async def heartbeat():
            try:
                while True:
                    await asyncio.sleep(ttl / 3)
                    if not await self.redis.eval(RENEW, 1, self.key, token, time.time() + ttl, time.time()):
                        raise ResourceLost()
                    await self.redis.expire(self.key, ttl * 2)
            except asyncio.CancelledError:
                raise
            except Exception:
                parent.cancel()

        task = asyncio.create_task(heartbeat())
        try:
            yield token
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            with contextlib.suppress(Exception):
                await self.redis.zrem(self.key, token)
