import asyncio
import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from echo.domain import JobState
from echo.engines import Adapter, DomainLimiter
from echo.worker import Worker


def runner_module():
    spec = importlib.util.spec_from_file_location("runner", Path("engines/runner.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.com/", "home"),
        ("https://example.com/login", "login"),
        ("https://example.com/search?q=x", "search"),
        ("https://example.com/Profile", "none"),
    ],
)
def test_redirect_classification(url, expected):
    assert (runner_module().redirect_kind(url, True) or "none") == expected
    assert runner_module().redirect_kind(url, False) is None


@pytest.fixture
def local_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/redirect/"):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            code = 200
            for name, status in [("absent", 404), ("blocked", 403), ("limited", 429), ("server", 503)]:
                if self.path.startswith("/" + name + "/"):
                    code = status
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if code == 429:
                self.send_header("Retry-After", "7")
            self.end_headers()
            self.wfile.write(b"PROFILE_ABSENT" if code == 404 else b"PROFILE_FOUND")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.skipif(
    os.getenv("ECHO_TEST_LIVE_ENGINES") != "1", reason="opt-in real engine/local socket contract"
)
@pytest.mark.parametrize("engine,version", [("sherlock", "0.16.2"), ("maigret", "0.6.6")])
@pytest.mark.parametrize(
    "path,code",
    [
        ("/found", 200),
        ("/absent", 404),
        ("/blocked", 403),
        ("/limited", 429),
        ("/server", 503),
        ("/redirect", 200),
    ],
)
def test_real_pinned_libraries_against_local_http(local_server, engine, version, path, code):
    python = os.getenv(f"ECHO_TEST_{engine.upper()}_PYTHON")
    if not python:
        python = str(Path(".venv-" + engine) / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    assert Path(python).exists(), f"Install the pinned {engine} environment"
    result = subprocess.run(
        [python, "tests/engine_fixture.py"],
        input=json.dumps({"engine": engine, "base": local_server, "path": path}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    assert record["http_code"] == code and record["version"] == version
    assert record["engine"] == engine and record["evidence"] is None
    if path == "/found":
        assert record["raw_status"] == "Claimed"
    if path == "/absent":
        assert record["raw_status"] == "Available"
    if path == "/limited":
        assert record["retry_after"] == "7"
    if path == "/redirect":
        assert record["redirect_kind"] == "login" and record["final_url"].endswith("/login")


async def test_worker_keeps_success_if_other_engine_fails(jobs, config, redis, observation, monkeypatch):
    await jobs.user(1)
    await jobs.create(1, "echo_fixture_user", ["sherlock", "maigret"], "curated", False, "worker1")
    job = await jobs.claim()

    async def run(self, job, skip, on_result, on_progress):
        if self.engine == "maigret":
            from echo.engines import EngineError

            raise EngineError("timeout")
        await on_result(observation)
        await on_progress({"completed": 1, "total": 1, "matches": 1, "errors": 0})

    monkeypatch.setattr(Adapter, "run", run)
    catalog = SimpleNamespace(aliases=AsyncMock(return_value={"github.com": "github"}))
    await Worker(config, jobs, redis, catalog).execute(job)
    final = await jobs.owned(1, job.id)
    assert final.state == JobState.PARTIAL and len(await jobs.cards(1, job.id)) == 1


async def test_running_worker_cancel_stops_adapter(jobs, config, redis, monkeypatch):
    await jobs.user(1)
    await jobs.create(1, "echo_fixture_user", ["sherlock"], "curated", False, "worker2")
    job = await jobs.claim()
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def run(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(Adapter, "run", run)
    catalog = SimpleNamespace(aliases=AsyncMock(return_value={}))
    task = asyncio.create_task(Worker(config, jobs, redis, catalog).execute(job))
    await entered.wait()
    await jobs.cancel(1, job.id)
    await asyncio.wait_for(task, 8)
    assert stopped.is_set()
    assert (await jobs.owned(1, job.id)).state == JobState.CANCELLED


async def test_subprocess_adapter_protocol_and_cancellation(config, redis, tmp_path, monkeypatch):
    bridge = tmp_path / "bridge.py"
    bridge.write_text(
        "import sys,json,time\nrequest=json.loads(sys.stdin.readline())\n"
        'print(json.dumps({"type":"ready","total":0,"version":"fixture"}),flush=True)\n'
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("echo.engines.RUNNER", bridge)
    config.sherlock_python = sys.executable
    adapter = Adapter(
        __import__("echo.domain", fromlist=["Engine"]).Engine.SHERLOCK, config, DomainLimiter(redis, 0.1)
    )
    started = asyncio.Event()

    async def progress(info):
        started.set()

    job = SimpleNamespace(username="echo_fixture_user", coverage="curated", targets=None)
    task = asyncio.create_task(adapter.run(job, [], AsyncMock(), progress))
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert not list(tmp_path.glob("engine-*"))


@pytest.mark.skipif(os.getenv("ECHO_TEST_LIVE_ENGINES") != "1", reason="real isolated bridge")
@pytest.mark.parametrize("engine", ["sherlock", "maigret"])
async def test_real_bridge_limits_and_empty_exact_selection(engine, config, redis):
    from echo.domain import Engine

    python = os.getenv(f"ECHO_TEST_{engine.upper()}_PYTHON")
    if not python:
        python = str(Path(".venv-" + engine) / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    resolved = await asyncio.to_thread(Path(python).absolute)
    setattr(config, engine + "_python", str(resolved))
    adapter = Adapter(Engine(engine), config, DomainLimiter(redis, 0.1))
    job = SimpleNamespace(username="echo_fixture_user", coverage="retry", targets={engine: []})
    records, info = [], []

    async def result(o):
        records.append(o)

    async def progress(p):
        info.append(dict(p))

    await adapter.run(job, [], result, progress)
    assert not records and info[0]["total"] == 0 and info[0]["version"] in {"0.16.2", "0.6.6"}
