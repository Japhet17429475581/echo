import asyncio
import hashlib
import io
import json
import os
import socket
import ssl
import subprocess
import threading
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.methods import GetFile, SendDocument
from aiogram.types import File, Update
from sqlalchemy import func, select
from test_smoke import LocalTelegramSession

from echo.admin import Admin
from echo.ai import Gateway
from echo.catalog import seed_catalog
from echo.export import Reports
from echo.modules import REGISTRY, LocalRunner, ModuleError, validate_input
from echo.packages import package
from echo.resources import HeavySlots
from echo.services import Denied, StorageLimited
from echo.sources import SafeNetwork, UnsafeSource
from echo.storage import AICall
from echo.telegram import Telegram
from echo.worker import Worker

native = pytest.mark.skipif(
    os.getenv("ECHO_TEST_TOOLS") != "1", reason="real Linux toolchain and kernel sandbox"
)


@pytest.fixture
def tool_fixtures(tmp_path):
    python = os.getenv("ECHO_TOOLS_PYTHON", "/opt/tools/bin/python")
    subprocess.run([python, "tests/make_tool_fixtures.py", str(tmp_path)], check=True, capture_output=True)
    return tmp_path


class FixtureResolver:
    async def resolve(self, host, port=0, family=0):
        assert host == "fixture.test"
        return [
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self):
        pass


class FixtureDNS:
    async def records(self, host, types=("A", "AAAA", "MX", "NS", "TXT")):
        return {
            kind: {
                "status": "ANSWER" if kind == "A" else "NO_ANSWER",
                "records": ["93.184.216.34"] if kind == "A" else [],
            }
            for kind in types
        }


@pytest.fixture
def fixture_site(tmp_path, monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        requests = []

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.requests.append(self.path)
            if self.path == "/private-redirect":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1/never")
                self.end_headers()
                return
            if self.path == "/large":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"x" * 1100000)
                return
            body = (
                '<!doctype html><html><head><title>ECHO article</title><meta name="generator" content="WordPress 6.0">'
                '<meta name="author" content="Unverified Fixture Author"></head><body><article><h1>ECHO local fixture</h1>'
                + "<p>This is original ECHO fixture text for extracting a meaningful article without contacting any external website. Links and scripts must never be executed by the tools.</p>"
                * 12
                + '<script>fetch("http://127.0.0.1/never-script")</script></article></body></html>'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    cert, key = tmp_path / "fixture.crt", tmp_path / "fixture.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=fixture.test",
            "-addext",
            "subjectAltName=DNS:fixture.test",
        ],
        check=True,
        capture_output=True,
    )
    http = ThreadingHTTPServer(("127.0.0.1", 80), Handler)
    https = ThreadingHTTPServer(("127.0.0.1", 443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    https.socket = context.wrap_socket(https.socket, server_side=True)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (http, https)]
    for thread in threads:
        thread.start()
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    import echo.sources

    public = echo.sources.public_ip
    # Inject the owned loopback fixture only for this test; URL guards remain enabled.
    monkeypatch.setattr(echo.sources, "public_ip", lambda value: value == "127.0.0.1" or public(value))
    yield Handler
    for server in (http, https):
        server.shutdown()
        server.server_close()
    for thread in threads:
        thread.join()


class UploadSession(LocalTelegramSession):
    def __init__(self):
        super().__init__()
        self.uploads = {}

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
        if isinstance(method, GetFile):
            return File(
                file_id=method.file_id,
                file_unique_id=method.file_id,
                file_size=len(self.uploads[method.file_id]),
                file_path="fixtures/" + method.file_id,
            )
        return await super().make_request(bot, method, timeout)

    async def stream_content(self, url, **kwargs):
        data = self.uploads[url.rsplit("/", 1)[-1]]
        for start in range(0, len(data), 65536):
            yield data[start : start + 65536]


@native
async def test_six_modules_from_telegram_to_real_results_and_workspace(
    tool_fixtures, fixture_site, jobs, config, redis, sessions, tmp_path, observation
):
    await seed_catalog(sessions)
    config.tools_python = "/opt/tools/bin/python"
    gateway = Gateway(config, redis, sessions)
    ui = Telegram(config, jobs, redis, Reports(jobs, config), gateway, Admin(sessions, redis, config))
    transport = UploadSession()
    bot = Bot(
        "123456789:TEST_TOKEN_NOT_REAL", session=transport, default=DefaultBotProperties(parse_mode="HTML")
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(ui.router)
    worker = Worker(config, jobs, redis, ui.extras.tasks.modules)  # search catalogue is unused by module jobs
    worker.tasks.modules.network = SafeNetwork(config, worker.limiter, resolver=FixtureResolver())
    worker.tasks.modules.dns = FixtureDNS()
    capabilities = await worker.tasks.modules.runner.probe()
    assert all(capabilities["available"].values())
    await redis.set("echo:tools:smoke", json.dumps(capabilities), ex=1800)
    sequence = 0

    async def message(text=None, file=None):
        nonlocal sequence
        sequence += 1
        payload = {
            "message_id": sequence,
            "date": 1700000000,
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 1, "is_bot": False, "first_name": "Fixture"},
        }
        if file:
            transport.uploads[file.name] = file.read_bytes()
            payload["document"] = {
                "file_id": file.name,
                "file_unique_id": file.name,
                "file_name": file.name,
                "file_size": file.stat().st_size,
            }
        else:
            payload["text"] = text
        await dispatcher.feed_update(
            bot, Update.model_validate({"update_id": sequence, "message": payload}, context={"bot": bot})
        )

    async def callback(data):
        nonlocal sequence
        assert len(data.encode()) <= 64
        sequence += 1
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                {
                    "update_id": sequence,
                    "callback_query": {
                        "id": str(sequence),
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

    async def execute_pending():
        job = await jobs.claim()
        assert job is not None
        await worker.execute(job)
        final = await jobs.owned(1, job.id)
        assert final.state == "COMPLETED", final.failures
        await callback("xr:" + job.id)
        return final

    await message("/start")
    results = {}
    measured = {}
    for module in REGISTRY:
        if module == "metadata":
            await callback("jg:file")
        else:
            await message("/tools")
            await callback("xt:choose:" + module)
        if module in {"metadata", "ocr", "qr"}:
            filename = {"metadata": "metadata.jpg", "ocr": "ocr.png", "qr": "qr.png"}[module]
            await message(file=tool_fixtures / filename)
            if module == "metadata":
                await callback("xt:uploaded:metadata")
        else:
            await message("echo-example.com" if module == "domains" else "http://fixture.test/article")
        draft = json.loads(await redis.get("echo:user:1:tool_draft"))
        await callback("xt:run:" + draft["nonce"])
        final = await execute_pending()
        results[module] = final
        measured[module] = final.metrics
        artifacts = await ui.extras.vault.for_job(1, final.id, "output")
        assert any(a.name == "report.json" for a in artifacts)
        await callback("xf:" + artifacts[0].id)
    metadata = results["metadata"].result["data"]
    assert metadata["sensitive"] and not any("gps" in k.lower() for k in metadata["fields"])
    assert not any("Directory" in k or k.startswith("System:") for k in metadata["fields"])
    assert "+03:00" in str(metadata["dates_as_stored"])
    await callback("xm:" + results["metadata"].id + ":1")
    await callback("xclean:" + results["metadata"].id)
    draft = json.loads(await redis.get("echo:user:1:tool_draft"))
    await callback("xt:run:" + draft["nonce"])
    clean = await execute_pending()
    assert clean.result["data"]["cleanup"]["verified"]
    assert (clean.result["data"]["cleanup"]["width"], clean.result["data"]["cleanup"]["height"]) == (180, 320)
    assert (
        "12345" in results["ocr"].result["data"]["text"]
        and "ПРОВЕРКА" in results["ocr"].result["data"]["text"]
    )
    assert results["ocr"].result["data"]["omp_thread_limit"] == 1
    assert results["ocr"].result["data"]["openmp_reported_by_build"] is True
    assert results["qr"].result["data"]["count"] == 3
    assert sum(c["url"] is not None for c in results["qr"].result["data"]["codes"]) == 1
    # Real QR -> passport -> page requires a fresh explicit confirmation at every hop.
    qr_index = next(i for i, c in enumerate(results["qr"].result["data"]["codes"]) if c["url"])
    await callback(f"jn:{results['qr'].id}:q:{qr_index}")
    recent = json.loads(await redis.get("echo:user:1:recent"))
    await callback("jr:again:" + recent["nonce"])
    draft = json.loads(await redis.get("echo:user:1:tool_draft"))
    assert draft["module"] == "passport"
    await callback("xt:run:" + draft["nonce"])
    linked_passport = await execute_pending()
    await callback(f"jn:{linked_passport.id}:p")
    recent = json.loads(await redis.get("echo:user:1:recent"))
    await callback("jr:again:" + recent["nonce"])
    draft = json.loads(await redis.get("echo:user:1:tool_draft"))
    assert draft["module"] == "page"
    await callback("xt:run:" + draft["nonce"])
    linked_page = await execute_pending()
    assert linked_page.result["data"]["text"] == results["page"].result["data"]["text"]
    assert results["passport"].result["data"]["tls"]["verified"]
    assert any(t["technology"] == "WordPress" for t in results["passport"].result["data"]["technologies"])
    assert results["page"].result["data"]["metadata"]["provenance"].endswith("unverified")
    for artifact in await ui.extras.vault.for_job(1, results["page"].id, "output"):
        if artifact.name == "page.html":
            data, _ = await ui.extras.vault.read(1, artifact.id)
            assert b"<script>" not in data and b"cdn." not in data
    assert results["domains"].result["data"]["dns_queries"] <= config.domain_dns_queries
    assert set(fixture_site.requests) == {"/article"}
    # Explicit workspace storage and exported checksums.
    await message("/folders")
    await callback("wf:create")
    await message("Моя рабочая папка")
    folder = (await ui.extras.folders.list(1))[0]
    await callback("fs:" + results["metadata"].id)
    pending = json.loads(await redis.get("echo:user:1:folder_material"))
    await callback(f"wp:{pending['nonce']}:{folder.id}")
    await callback("wf:note:" + folder.id)
    await message("Собственная заметка")
    await callback("wf:file:" + folder.id)
    await message(file=tool_fixtures / "own-notes.txt")
    await callback("wf:rename:" + folder.id)
    await message("Переименованная папка")
    assert len(await ui.extras.folders.items(1, folder.id)) == 3
    # Deleting one source must preserve explicitly saved folder copies and other jobs.
    await callback("dj:" + results["metadata"].id)
    confirmation = json.loads(await redis.get("echo:user:1:delete_job"))
    await callback("deletejob:" + confirmation["token"])
    assert (await jobs.owned(1, results["ocr"].id)).result
    for item in await ui.extras.folders.items(1, folder.id):
        for file_id in item.data.get("file_ids", []):
            assert await ui.extras.vault.read(1, file_id)
    await callback("wf:export:" + folder.id)
    export = await execute_pending()
    archives = await ui.extras.vault.for_job(1, export.id, "output")
    archive = next(f for f in archives if f.name.endswith(".zip"))
    binary, _ = await ui.extras.vault.read(1, archive.id)
    with zipfile.ZipFile(io.BytesIO(binary)) as z:
        manifest = json.loads(z.read("manifest.json"))
        for record in manifest["files"]:
            assert hashlib.sha256(z.read(record["path"])).hexdigest() == record["sha256"]
            assert ".." not in record["path"].split("/")
    # Standard age public-key confirmation and decryption of the actual workspace ZIP.
    identity = tmp_path / "identity.txt"
    await asyncio.to_thread(
        subprocess.run, ["age-keygen", "-o", str(identity)], check=True, capture_output=True
    )
    key = await asyncio.to_thread(
        subprocess.run, ["age-keygen", "-y", str(identity)], check=True, capture_output=True, text=True
    )
    await message("/settings")
    await callback("pref:key")
    await message(key.stdout.strip())
    await callback("wf:age:" + folder.id)
    state = json.loads(await redis.get("echo:user:1:package_recipient"))
    await callback("wage:" + state["nonce"])
    encrypted = await execute_pending()
    age_file = next(
        f for f in await ui.extras.vault.for_job(1, encrypted.id, "output") if f.name.endswith(".age")
    )
    ciphertext, _ = await ui.extras.vault.read(1, age_file.id)
    plain = await asyncio.to_thread(
        subprocess.run, ["age", "-d", "-i", str(identity)], input=ciphertext, capture_output=True, check=True
    )
    assert zipfile.is_zipfile(io.BytesIO(plain.stdout))
    # Compare two manually performed checks through UI, with ownership confirmation.
    await message("/tools")
    await callback("xt:choose:passport")
    await message("http://fixture.test/article")
    recent = json.loads(await redis.get("echo:user:1:recent"))
    await callback("jr:again:" + recent["nonce"])
    draft = json.loads(await redis.get("echo:user:1:tool_draft"))
    await callback("xt:run:" + draft["nonce"])
    second = await execute_pending()
    await message("/compare")
    await callback("xc:first:" + results["passport"].id)
    await callback("xc:second:" + second.id)
    state = json.loads(await redis.get("echo:user:1:comparison"))
    await callback("xc:run:" + state["nonce"])
    compared = await execute_pending()
    assert compared.result["difference"]["unchanged"]
    # PDF uses the same isolated, queued resource slot as OCR.
    search = await jobs.create(1, "echo_fixture_user", ["sherlock"], "curated", False, "pdf-source")
    leased = await jobs.claim()
    await jobs.save(leased.id, leased.lease, observation, "github")
    await jobs.finish(leased.id, leased.lease, {})
    await callback(f"x:{search.id}:pdf:0")
    pdf_job = await execute_pending()
    pdf_file = (await ui.extras.vault.for_job(1, pdf_job.id, "output"))[0]
    pdf_bytes, _ = await ui.extras.vault.read(1, pdf_file.id)
    assert pdf_bytes.startswith(b"%PDF")
    # History changes must keep job-associated files available for the same TTL.
    await jobs.settings(1, history=True)
    retained = await jobs.owned(1, pdf_job.id)
    file_retained = await ui.extras.vault.get(1, pdf_file.id)
    assert file_retained.expires_at == retained.expires_at
    await jobs.settings(1, history=False)
    snapshot = await ui.admin.snapshot()
    json.dumps(snapshot)  # PostgreSQL SUM(bigint) returns Decimal; API output must remain JSON-safe.
    assert set(results).issubset(snapshot["task_metrics_retained"])
    assert snapshot["stored_or_reserved_bytes"] > 0
    assert snapshot["task_metrics_retained"]["ocr"]["peak_tree_rss_bytes_sampled"] > 0
    # Low space blocks admission, but must never block orphan cleanup.
    original_floor = config.disk_min_free_mb
    config.disk_min_free_mb = 10**12
    orphan = config.data_dir / uuid.uuid4().hex
    await asyncio.to_thread(orphan.write_bytes, b"abandoned fixture")
    try:
        with pytest.raises(StorageLimited):
            await ui.extras.vault.space()
        await ui.extras.vault.janitor()
        assert not orphan.exists()
        await ui.extras.vault.read(1, pdf_file.id)
    finally:
        config.disk_min_free_mb = original_floor
    await message("/account")
    assert any(isinstance(c, SendDocument) for c in transport.calls)
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(AICall)) == 0
    await jobs.user(2)
    with pytest.raises(Denied):
        await ui.extras.folders.owned(2, folder.id)
    with pytest.raises(Denied):
        await ui.extras.vault.read(2, archive.id)
    await message("/delete_my_data")
    nonce = json.loads(await redis.get("echo:user:1:delete"))
    await callback("delete:" + nonce)
    await ui.extras.vault.janitor()
    assert list(config.data_dir.iterdir()) == []
    assert not list(config.temp_dir.glob("module-*"))
    print("ECHO_TOOL_RESOURCE_SAMPLES=" + json.dumps(measured, sort_keys=True))
    await gateway.close()
    await bot.session.close()


@native
async def test_real_negative_files_and_missing_dependency(tool_fixtures, config, tmp_path):
    runner = LocalRunner(config)
    for module, filename, expected in [
        ("qr", "blank.png", None),
        ("ocr", "oversize.png", "image_limit"),
        ("ocr", "broken.png", "invalid_file"),
    ]:
        work = tmp_path / uuid.uuid4().hex
        work.mkdir()
        if expected:
            with pytest.raises(ModuleError, match=expected):
                await runner.execute(module, {}, work=work, source=tool_fixtures / filename)
        else:
            result, _ = await runner.execute(module, {}, work=work, source=tool_fixtures / filename)
            assert result["data"]["codes"] == []
    config.tools_python = "/missing/echo-tool-python"
    capabilities = await LocalRunner(config).probe()
    assert not any(capabilities["available"].values())


@native
async def test_real_network_policy_and_verified_tls(fixture_site, config):
    network = SafeNetwork(config, resolver=FixtureResolver())
    response = await network.fetch("http://fixture.test/article")
    assert response.status == 200 and b"ECHO" in response.body
    cert = await network.tls("fixture.test")
    assert cert["verified"] and len(cert["sha256"]) == 64
    with pytest.raises(UnsafeSource):
        await network.fetch("http://fixture.test/private-redirect")
    assert "/never" not in fixture_site.requests
    with pytest.raises(UnsafeSource):
        await network.fetch("http://fixture.test/large")


async def test_distributed_heavy_slot_and_cancellation(redis, config):
    config.heavy_slots = 1
    first, second = HeavySlots(redis, config), HeavySlots(redis, config)
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum = 0

    async def use(slots):
        nonlocal active, maximum
        async with slots.hold():
            active += 1
            maximum = max(maximum, active)
            entered.set()
            try:
                await release.wait()
            finally:
                active -= 1

    a = asyncio.create_task(use(first))
    await entered.wait()
    entered.clear()
    b = asyncio.create_task(use(second))
    await asyncio.sleep(0.1)
    assert not entered.is_set() and maximum == 1
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    await asyncio.wait_for(entered.wait(), 3)
    release.set()
    await b
    assert maximum == 1 and not await redis.zcard(first.key)


def test_domain_permission_input_and_archive_paths(config):
    with pytest.raises(ModuleError):
        validate_input("domains", {"target": "example.com"}, config)
    with pytest.raises(ValueError):
        validate_input("page", {"target": "http://169.254.169.254/latest/meta-data/"}, config)
    content = package(
        [("../../secret.txt", b"owned fixture", {"schema_version": "test/1"})], {"type": "test"}, config
    )
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert all(".." not in name.split("/") for name in archive.namelist())


@native
def test_kernel_denies_network_and_other_process_secrets(tmp_path):
    work = tmp_path / "isolated"
    work.mkdir()
    outside = tmp_path / "other-component-secret"
    outside.write_text("test sentinel, not a real secret")
    code = """
import json,sys,os,socket
sys.path.insert(0,'/app/tools')
from sandbox import isolate
isolate(os.getcwd())
checks={}
for label,path in [('outside',sys.argv[1]),('parent_environ','/proc/'+str(os.getppid())+'/environ')]:
 try:
  open(path,'rb').read();checks[label]=False
 except PermissionError: checks[label]=True
try:
 socket.socket();checks['network']=False
except PermissionError: checks['network']=True
open('allowed.txt','w').write('owned scratch')
checks['scratch']=True
print(json.dumps(checks))
"""
    result = subprocess.run(
        ["/opt/tools/bin/python", "-c", code, str(outside)],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    )
    assert all(json.loads(result.stdout).values())


@native
async def test_module_timeout_and_cancel_kill_tree(config, tmp_path, monkeypatch):
    import psutil

    bridge = tmp_path / "slow-bridge.py"
    bridge.write_text(
        "import sys,json,time,subprocess\njson.loads(sys.stdin.readline())\n"
        'subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\ntime.sleep(60)\n'
    )
    monkeypatch.setattr("echo.modules.TOOLS_RUNNER", bridge)
    config.ocr_timeout = 2
    runner = LocalRunner(config)
    existing = {p.pid for p in psutil.Process().children(recursive=True)}
    work = tmp_path / "deadline"
    work.mkdir()
    with pytest.raises(ModuleError, match="tool_timeout"):
        await runner.execute("ocr", {}, work=work)
    work = tmp_path / "cancel"
    work.mkdir()
    task = asyncio.create_task(runner.execute("ocr", {}, work=work))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    survivors = [
        p
        for p in psutil.Process().children(recursive=True)
        if p.pid not in existing and p.status() != psutil.STATUS_ZOMBIE
    ]
    assert survivors == []
