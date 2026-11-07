import argparse
import asyncio
import contextlib
import json
import logging
import signal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import select, text

from echo.admin import Admin
from echo.ai import Gateway
from echo.catalog import Catalog, seed_catalog
from echo.config import settings
from echo.domain import Category, now, safe_url
from echo.export import Reports
from echo.services import Jobs
from echo.storage import Site, session_factory
from echo.telegram import Telegram
from echo.worker import Worker


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{0,252}$")
    language: str = Field(pattern="^(ru|en)$")
    name: str = Field(min_length=1, max_length=120)
    domains: list[str] = Field(min_length=1, max_length=20)
    category: Category
    description: str = Field(max_length=400)
    activities: list[str] = Field(max_length=3)
    sources: list[str] = Field(min_length=1, max_length=5)
    human_reviewed: bool = True


def configure_logs():
    # Never install SDK/ORM request loggers, or emit Update/exception representations.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for name in ("aiogram", "sqlalchemy", "httpx", "httpcore", "aiohttp", "redis"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


async def run(args):
    config = settings()
    if args.command == "serve":
        if not config.bot_token.get_secret_value():
            logging.getLogger("echo").error("ECHO_BOT_TOKEN or BOT_TOKEN is required")
            raise ValueError("bot_token_missing")
        if "data_dir" not in config.model_fields_set:
            config.data_dir = Path("/app/data/echo")
        from echo.hosting import migrate

        await migrate()
    engine, sessions = session_factory(config.database_url)
    redis = Redis.from_url(config.redis_url, decode_responses=True)
    gateway = Gateway(config, redis, sessions)
    jobs = Jobs(sessions, config)
    catalog = Catalog(sessions, redis, gateway, config)
    admin = Admin(sessions, redis, config)
    try:
        if args.command == "seed":
            await seed_catalog(sessions)
        elif args.command == "health":
            async with sessions() as s:
                await s.execute(text("SELECT 1"))
            await redis.ping()
            if args.worker and not [key async for key in redis.scan_iter(match="echo:worker:*")]:
                raise RuntimeError("worker_not_alive")
            print("healthy")
        elif args.command == "admin":
            print(json.dumps(await admin.snapshot(), ensure_ascii=False, indent=2))
        elif args.command == "catalog-list":
            async with sessions() as s:
                for site in await s.scalars(select(Site).order_by(Site.id, Site.language)):
                    print(site.id, site.language, site.origin, site.name)
        elif args.command == "catalog-import":
            entries = json.loads(await asyncio.to_thread(Path(args.path).read_text, "utf-8"))
            from datetime import timedelta

            for item in entries:
                entry = CatalogEntry.model_validate(item)
                if any(not safe_url(u) for u in entry.sources):
                    raise ValueError("unsafe_source")
                for domain in entry.domains:
                    if not safe_url("https://" + domain) or "/" in domain:
                        raise ValueError("unsafe_domain")
                async with sessions.begin() as s:
                    site = await s.get(Site, (entry.id, entry.language), with_for_update=True)
                    if not site:
                        site = Site(id=entry.id, language=entry.language)
                        s.add(site)
                    for key in ("name", "domains", "category", "description", "activities", "sources"):
                        setattr(site, key, getattr(entry, key))
                    site.origin = "human" if entry.human_reviewed else "unavailable"
                    site.reviewed = "human_reviewed" if entry.human_reviewed else "pending"
                    site.checked_at, site.refresh_at = (
                        now(),
                        now() + timedelta(days=365 if entry.human_reviewed else -1),
                    )
        elif args.command == "catalog-refresh":
            await catalog.get(args.site_id, args.language, "catalog-admin", refresh=True)
        elif args.command == "cleanup":
            await jobs.cleanup()
            from echo.files import Vault

            await Vault(sessions, config).janitor()
        else:
            await seed_catalog(sessions)
            if args.command == "bot":
                await Telegram(config, jobs, redis, Reports(jobs, config), gateway, admin).run()
            elif args.command == "serve":
                from echo.hosting import supervise

                await redis.ping()
                logging.getLogger("echo").warning("starting_bot_and_worker")
                await supervise(
                    Telegram(config, jobs, redis, Reports(jobs, config), gateway, admin).run(),
                    Worker(config, jobs, redis, catalog).run(),
                )
            else:
                worker = asyncio.create_task(Worker(config, jobs, redis, catalog).run())
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    with contextlib.suppress(NotImplementedError):
                        loop.add_signal_handler(sig, worker.cancel)
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
    finally:
        await gateway.close()
        await redis.aclose()
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(prog="echo-bot")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("bot", "worker", "serve", "seed", "admin", "cleanup", "catalog-list"):
        commands.add_parser(name)
    health = commands.add_parser("health")
    health.add_argument("--worker", action="store_true")
    imp = commands.add_parser("catalog-import")
    imp.add_argument("path")
    refresh = commands.add_parser("catalog-refresh")
    refresh.add_argument("site_id")
    refresh.add_argument("--language", choices=["ru", "en"], default="ru")
    configure_logs()
    try:
        asyncio.run(run(parser.parse_args()))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        # Configuration/connection errors may embed secrets in their text.
        logging.getLogger("echo").error("startup_failed type=%s", type(e).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
