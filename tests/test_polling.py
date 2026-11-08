import asyncio
import contextlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import DeleteWebhook, GetMe, GetUpdates, GetWebhookInfo, SendMessage, SendPhoto
from aiogram.types import Update, User, WebhookInfo
from pydantic import SecretStr
from test_smoke import LocalTelegramSession

from echo.admin import Admin
from echo.ai import Gateway
from echo.export import Reports
from echo.i18n import tr
from echo.main import configure_logs
from echo.polling import PollingDiagnostics, prepare_polling
from echo.telegram import Telegram

TOKEN = "123456789:TEST_TOKEN_NOT_REAL"
PRIVATE = "do-not-log-sensitive-telegram-data"


class PollingSession(LocalTelegramSession):
    def __init__(self, *, conflict=False, unauthorized=False, chat_type="private"):
        super().__init__()
        self.conflict, self.unauthorized = conflict, unauthorized
        self.chat_type = chat_type
        self.delivered, self.closed = False, False
        self.answered = asyncio.Event()

    async def close(self):
        self.closed = True

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 -- SDK signature
        if isinstance(method, (GetMe, GetWebhookInfo, DeleteWebhook, GetUpdates)):
            self.calls.append(method)
        if isinstance(method, GetMe):
            if self.unauthorized:
                raise TelegramUnauthorizedError(method=method, message=PRIVATE)
            return User(id=bot.id, is_bot=True, first_name="Echo", username="EchoFixtureBot")
        if isinstance(method, GetWebhookInfo):
            return WebhookInfo(
                url="https://fixture.invalid/" + PRIVATE, has_custom_certificate=False, pending_update_count=7
            )
        if isinstance(method, DeleteWebhook):
            assert method.drop_pending_updates is False
            return True
        if isinstance(method, GetUpdates):
            if self.conflict:
                self.conflict = False
                raise TelegramConflictError(method=method, message=PRIVATE)
            if not self.delivered:
                self.delivered = True
                return [
                    Update.model_validate(
                        {
                            "update_id": 1,
                            "message": {
                                "message_id": 1,
                                "date": 1700000000,
                                "chat": {"id": 1, "type": self.chat_type},
                                "from": {"id": 1, "is_bot": False, "first_name": PRIVATE},
                                "text": "/start",
                            },
                        },
                        context={"bot": bot},
                    )
                ]
            await asyncio.sleep(0.02)
            return []
        result = await super().make_request(bot, method, timeout)
        if isinstance(method, (SendMessage, SendPhoto)):
            self.answered.set()
        return result


@pytest.mark.parametrize("chat_type", ["private", "group"])
async def test_real_polling_recovers_from_conflict_and_answers_start(
    config, jobs, sessions, redis, monkeypatch, caplog, chat_type
):
    configure_logs()
    config.bot_token = SecretStr(TOKEN)
    gateway = Gateway(config, redis, sessions)
    ui = Telegram(config, jobs, redis, Reports(jobs, config), gateway, Admin(sessions, redis, config))
    transport = PollingSession(conflict=True, chat_type=chat_type)
    bot = Bot(TOKEN, session=transport)
    dispatcher = Dispatcher()
    original_start = dispatcher.start_polling

    async def start(*args, **kwargs):
        kwargs["handle_signals"] = False
        await original_start(*args, **kwargs)

    monkeypatch.setattr(dispatcher, "start_polling", start)
    monkeypatch.setattr("echo.telegram.Bot", lambda *args, **kwargs: bot)
    monkeypatch.setattr("echo.telegram.Dispatcher", lambda: dispatcher)
    task = asyncio.create_task(ui.run())
    answer = asyncio.create_task(transport.answered.wait())
    try:
        with caplog.at_level(logging.INFO, logger="echo"):
            done, _ = await asyncio.wait({task, answer}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
            assert answer in done, "polling did not answer /start"
            await dispatcher.stop_polling()
            await asyncio.wait_for(task, 3)
        assert transport.closed
        assert isinstance(transport.calls[0], GetMe)
        assert isinstance(transport.calls[1], GetWebhookInfo)
        assert isinstance(transport.calls[2], DeleteWebhook)
        assert "telegram_connected username=@EchoFixtureBot" in caplog.text
        assert "stage=getUpdates error=TelegramConflictError" in caplog.text
        assert "telegram_polling_ready" in caplog.text
        assert PRIVATE not in caplog.text and TOKEN not in caplog.text
        reply = next(call for call in transport.calls if isinstance(call, (SendMessage, SendPhoto)))
        if chat_type == "private":
            assert reply.reply_markup.inline_keyboard
        else:
            assert reply.text == tr("ru", "private_only")
    finally:
        task.cancel()
        answer.cancel()
        await asyncio.gather(task, answer, return_exceptions=True)
        await gateway.close()


@pytest.mark.parametrize(
    "error_type", [TelegramConflictError, TelegramNetworkError, TelegramUnauthorizedError]
)
async def test_request_errors_log_codes_without_telegram_content(error_type, caplog):
    method = GetUpdates()
    observer = PollingDiagnostics()
    request = AsyncMock(side_effect=error_type(method=method, message=PRIVATE + TOKEN))
    with caplog.at_level(logging.INFO, logger="echo"), pytest.raises(error_type):
        await observer(request, None, method)
    assert "stage=getUpdates error=" + error_type.__name__ in caplog.text
    assert PRIVATE not in caplog.text and TOKEN not in caplog.text


async def test_polling_session_closes_when_token_is_rejected(config, jobs, sessions, redis, monkeypatch):
    config.bot_token = SecretStr(TOKEN)
    gateway = Gateway(config, redis, sessions)
    ui = Telegram(config, jobs, redis, Reports(jobs, config), gateway, Admin(sessions, redis, config))
    transport = PollingSession(unauthorized=True)
    bot = Bot(TOKEN, session=transport)
    monkeypatch.setattr("echo.telegram.Bot", lambda *args, **kwargs: bot)
    try:
        with pytest.raises(TelegramUnauthorizedError):
            await ui.run()
        assert transport.closed and len(transport.calls) == 1
    finally:
        await gateway.close()


async def test_startup_leaves_absent_webhook_alone():
    bot = SimpleNamespace(
        me=AsyncMock(return_value=SimpleNamespace(username="EchoFixtureBot")),
        get_webhook_info=AsyncMock(
            return_value=WebhookInfo(url="", has_custom_certificate=False, pending_update_count=0)
        ),
        delete_webhook=AsyncMock(),
    )
    await prepare_polling(bot)
    bot.delete_webhook.assert_not_awaited()


async def test_polling_readiness_reports_initial_connection_and_recovery(caplog):
    observer = PollingDiagnostics()
    method = GetUpdates()
    request = AsyncMock(side_effect=[[], [], TelegramNetworkError(method=method, message=PRIVATE), [], []])
    with caplog.at_level(logging.INFO, logger="echo"):
        for _ in range(5):
            with contextlib.suppress(TelegramNetworkError):
                await observer(request, None, method)
    assert caplog.text.count("telegram_polling_ready") == 2
    assert PRIVATE not in caplog.text
