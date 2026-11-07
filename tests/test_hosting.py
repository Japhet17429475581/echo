import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import SecretStr

from echo.config import Settings
from echo.hosting import supervise
from echo.paths import ROOT


def test_runtime_bundle_contains_migrations_bridges_and_brand_images():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from echo.engines import RUNNER
    from echo.modules import TOOLS_RUNNER
    from echo.tools_ui import ASSETS, TOOL_ASSETS

    assert RUNNER.is_file() and TOOLS_RUNNER.is_file()
    assert (TOOLS_RUNNER.parent / "sandbox.py").is_file()
    assert all((ASSETS / name).is_file() for name in TOOL_ASSETS.values())
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1
    assert scripts.get_revision("head") is not None


async def test_supervisor_stops_worker_after_bot_stops():
    ready, closed = asyncio.Event(), asyncio.Event()

    async def bot():
        await ready.wait()

    async def worker():
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    await asyncio.wait_for(supervise(bot(), worker()), 2)
    assert closed.is_set()


async def test_serve_migrates_then_runs_existing_bot_and_worker(monkeypatch, config, sessions, redis):
    from echo import main as application

    config.bot_token = SecretStr("hosting-fixture-not-a-real-token")
    migration = AsyncMock()
    worker_ready, worker_closed = asyncio.Event(), asyncio.Event()

    async def bot_run(self):
        migration.assert_awaited_once()
        await worker_ready.wait()

    async def worker_run(self):
        migration.assert_awaited_once()
        worker_ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            worker_closed.set()

    monkeypatch.setattr(application, "settings", lambda: config)
    monkeypatch.setattr(application, "session_factory", lambda url: (sessions.kw["bind"], sessions))
    monkeypatch.setattr(application.Redis, "from_url", lambda *a, **kw: redis)
    monkeypatch.setattr("echo.hosting.migrate", migration)
    monkeypatch.setattr(application.Telegram, "run", bot_run)
    monkeypatch.setattr(application.Worker, "run", worker_run)
    await asyncio.wait_for(application.run(SimpleNamespace(command="serve")), 5)
    assert worker_closed.is_set()


def test_host_token_alias_and_explicit_override(monkeypatch):
    monkeypatch.delenv("ECHO_BOT_TOKEN", raising=False)
    monkeypatch.setenv("BOT_TOKEN", "hosting-fixture")
    assert Settings(_env_file=None).bot_token.get_secret_value() == "hosting-fixture"
    monkeypatch.setenv("ECHO_BOT_TOKEN", "echo-fixture")
    assert Settings(_env_file=None).bot_token.get_secret_value() == "echo-fixture"
    monkeypatch.setenv("ECHO_BOT_TOKEN", "")
    assert Settings(_env_file=None).bot_token.get_secret_value() == "hosting-fixture"
    assert (
        Settings(_env_file=None, bot_token=SecretStr("explicit-fixture")).bot_token.get_secret_value()
        == "explicit-fixture"
    )


def test_root_launcher_works_from_another_directory(tmp_path):
    launcher = Path(__file__).resolve().parents[1] / "main.py"
    result = subprocess.run(
        [sys.executable, str(launcher), "--help"], cwd=tmp_path, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0 and "serve" in result.stdout
    assert "ModuleNotFoundError" not in result.stderr
