"""Single-container startup over the existing bot, worker, queue and storage."""

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]


async def migrate():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(ROOT / "src"))
    await asyncio.to_thread(command.upgrade, config, "head")


async def supervise(bot, worker):
    tasks = [asyncio.create_task(bot), asyncio.create_task(worker)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
