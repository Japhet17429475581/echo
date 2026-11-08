"""Polling startup and request diagnostics without logging Telegram tokens or updates."""

import asyncio
import logging

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import GetUpdates

from echo.diagnostics import failure

log = logging.getLogger("echo")


class PollingDiagnostics(BaseRequestMiddleware):
    def __init__(self):
        self.ready = False

    async def __call__(self, make_request, bot, method):
        try:
            result = await make_request(bot, method)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if isinstance(method, GetUpdates):
                self.ready = False
            failure(log, "telegram_request_failed", method.__api_method__, error)
            raise
        if isinstance(method, GetUpdates) and not self.ready:
            self.ready = True
            log.info("telegram_polling_ready")
        return result


async def prepare_polling(bot):
    stage = "getMe"
    try:
        async with asyncio.timeout(15):
            identity = await bot.me()
            log.info("telegram_connected username=@%s", identity.username or "unknown")
            stage = "getWebhookInfo"
            webhook = await bot.get_webhook_info()
            if webhook.url:
                stage = "deleteWebhook"
                await bot.delete_webhook(drop_pending_updates=False)
                log.info("telegram_webhook_removed pending_updates_preserved=true")
        log.info("telegram_polling_starting")
    except TimeoutError as error:
        failure(log, "telegram_startup_failed", stage, error)
        raise
