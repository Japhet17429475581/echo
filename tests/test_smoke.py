import json
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendDocument
from aiogram.types import Message, Update

from echo.admin import Admin
from echo.ai import Gateway
from echo.catalog import seed_catalog
from echo.export import Reports
from echo.telegram import Telegram


class LocalTelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.counter = 100

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 -- upstream SDK signature
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        self.counter += 1
        return Message(
            message_id=getattr(method, "message_id", None) or self.counter,
            date=datetime.now(UTC),
            chat={"id": 1, "type": "private"},
            from_user={"id": bot.id, "is_bot": True, "first_name": "Echo"},
            text=getattr(method, "text", None),
        )


async def test_telegram_complete_path_without_ai(jobs, config, redis, sessions, observation):
    await seed_catalog(sessions)
    gateway = Gateway(config, redis, sessions)
    ui = Telegram(config, jobs, redis, Reports(jobs, config), gateway, Admin(sessions, redis, config))
    transport = LocalTelegramSession()
    bot = Bot(
        "123456789:TEST_TOKEN_NOT_REAL", session=transport, default=DefaultBotProperties(parse_mode="HTML")
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(ui.router)
    counter = 0

    async def message(text):
        nonlocal counter
        counter += 1
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                {
                    "update_id": counter,
                    "message": {
                        "message_id": counter,
                        "date": 1700000000,
                        "chat": {"id": 1, "type": "private"},
                        "from": {"id": 1, "is_bot": False, "first_name": "Fixture"},
                        "text": text,
                    },
                },
                context={"bot": bot},
            ),
        )

    async def callback(data):
        nonlocal counter
        counter += 1
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                {
                    "update_id": counter,
                    "callback_query": {
                        "id": str(counter),
                        "chat_instance": "fixture",
                        "from": {"id": 1, "is_bot": False, "first_name": "Fixture"},
                        "data": data,
                        "message": {
                            "message_id": 150,
                            "date": 1700000000,
                            "chat": {"id": 1, "type": "private"},
                            "from": {"id": bot.id, "is_bot": True, "first_name": "Echo"},
                            "text": "View",
                        },
                    },
                },
                context={"bot": bot},
            ),
        )

    await message("/start")
    await message("@echo_fixture_user")
    await callback("cfg:e:both")
    await callback("cfg:c:curated")
    await callback("cfg:run")
    await callback("cfg:run")
    stored = await jobs.list(1)
    assert len(stored) == 1
    job = await jobs.claim()
    await jobs.save(job.id, job.lease, observation, "github")
    await jobs.finish(job.id, job.lease, {"maigret": "fixture_failure"})
    await message("/tasks")
    await callback(f"r:{job.id}:a:0")
    card = (await jobs.cards(1, job.id))[0]
    prefix = card["key"][:12]
    await callback(f"c:{job.id}:{prefix}")
    await callback(f"why:{job.id}:{prefix}")
    await callback(f"m:{job.id}:{prefix}:u")
    await callback(f"n:{job.id}:{prefix}")
    await message("Полезная заметка")
    await message("Скачать JSON")
    assert any(isinstance(call, SendDocument) for call in transport.calls)
    assert (await jobs.cards(1, job.id))[0]["note"] == "Полезная заметка"
    await message("/settings")
    await callback("pref:language")
    assert (await jobs.user(1)).language == "en"
    await message("/delete_my_data")
    nonce = json.loads(await redis.get("echo:user:1:delete"))
    await callback("delete:" + nonce)
    assert await jobs.list(1) == []
    assert [key async for key in redis.scan_iter(match="echo:user:1:*")] == []
    await gateway.close()
    await bot.session.close()


async def test_deleted_generation_cannot_recreate_ui(jobs, config, redis, sessions):
    import pytest

    from echo.services import Denied

    gateway = Gateway(config, redis, sessions)
    ui = Telegram(config, jobs, redis, Reports(jobs, config), gateway, Admin(sessions, redis, config))
    u = await jobs.user(1)
    ui.generation.set(u.generation)
    await ui.put(1, "draft", {"username": "private_fixture"})
    await jobs.delete_user(1, clear=lambda: ui.clear_ui(1))
    await jobs.user(1)
    with pytest.raises(Denied):
        await ui.put(1, "draft", {"username": "private_fixture"})
    assert [key async for key in redis.scan_iter(match="echo:user:1:*")] == []
    await gateway.close()
