"""Exercise single-container migration/worker startup without contacting Telegram."""

import asyncio
import os

from echo.main import run
from echo.modules import REGISTRY
from echo.telegram import Telegram


async def local_polling(self):
    async with asyncio.timeout(30):
        for _ in range(60):
            snapshot = await self.admin.snapshot()
            if snapshot["workers"] == 1 and set(REGISTRY) <= set(snapshot["available_modules"]):
                print("hosting smoke: migrations, shared bot/worker services and all six tools ready")
                return
            await asyncio.sleep(0.5)
    raise RuntimeError("hosting_worker_not_ready")


if __name__ == "__main__":
    from types import SimpleNamespace

    os.environ["BOT_TOKEN"] = "123456789:TEST_TOKEN_NOT_REAL"
    os.environ["ECHO_AI_ENABLED"] = "false"
    Telegram.run = local_polling
    asyncio.run(run(SimpleNamespace(command="serve")))
