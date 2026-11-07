import asyncio
import csv
import io
import json
import os
from datetime import timedelta

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Update
from pypdf import PdfReader
from test_smoke import LocalTelegramSession

from echo.admin import Admin
from echo.ai import Gateway
from echo.coverage import search_coverage
from echo.domain import Status, now
from echo.export import Reports
from echo.modules import REGISTRY
from echo.services import Denied, Limited
from echo.storage import Job
from echo.telegram import Telegram


@pytest.fixture
async def client(jobs, config, redis, sessions):
    gateway = Gateway(config, redis, sessions)
    reports = Reports(jobs, config)
    ui = Telegram(config, jobs, redis, reports, gateway, Admin(sessions, redis, config))
    transport = LocalTelegramSession()
    bot = Bot(
        "123456789:TEST_TOKEN_NOT_REAL", session=transport, default=DefaultBotProperties(parse_mode="HTML")
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(ui.router)
    counter = 0

    async def feed(text=None, data=None, owner=1):
        nonlocal counter
        counter += 1
        sender = {"id": owner, "is_bot": False, "first_name": "Fixture"}
        msg = {
            "message_id": counter,
            "date": 1700000000,
            "chat": {"id": owner, "type": "private"},
            "from": sender,
            "text": text or "View",
        }
        payload = {"update_id": counter, "message": msg}
        if data:
            assert len(data.encode()) <= 64
            payload = {
                "update_id": counter,
                "callback_query": {
                    "id": str(counter),
                    "chat_instance": "fixture",
                    "from": sender,
                    "data": data,
                    "message": msg,
                },
            }
        await dispatcher.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))
        for call in transport.calls:
            markup = getattr(call, "reply_markup", None)
            if markup:
                assert all(
                    len(b.callback_data.encode()) <= 64
                    for row in markup.inline_keyboard
                    for b in row
                    if b.callback_data
                )

    await redis.set("echo:tools:fixture", json.dumps({"available": {m: True for m in REGISTRY}}))
    try:
        yield ui, transport, feed
    finally:
        await gateway.close()
        await bot.session.close()


async def search_job(jobs, observation):
    await jobs.user(1)
    job = await jobs.create(1, "echo_fixture_user", ["sherlock", "maigret"], "curated", False, "source")
    leased = await jobs.claim()
    await jobs.save(job.id, leased.lease, observation, "github")
    await jobs.save(
        job.id,
        leased.lease,
        observation.model_copy(
            update={
                "site_key": "BlockedFixture",
                "site_name": "Blocked fixture",
                "domain": "example.org",
                "url": "https://example.org/echo_fixture_user",
                "status": Status.BLOCKED,
                "http_code": 403,
            }
        ),
        "fixture",
    )
    await jobs.progress(job.id, leased.lease, "sherlock", {"total": 3, "completed": 2})
    await jobs.progress(job.id, leased.lease, "maigret", {"total": 1, "completed": 0})
    await jobs.finish(job.id, leased.lease, {"maigret": "timeout"})
    return await jobs.owned(1, job.id)


async def test_purposes_recent_batch_and_review_through_telegram(client, jobs, redis, observation):
    ui, transport, feed = client
    job = await search_job(jobs, observation)
    await feed("/start")
    assert any(
        b.callback_data == "jg:site"
        for c in transport.calls
        if getattr(c, "reply_markup", None)
        for r in c.reply_markup.inline_keyboard
        for b in r
    )
    await feed(data="jg:nick")
    await feed("@echo_fixture_user")
    recent = await ui.get(1, "recent")
    assert recent["recent_job"] == job.id and len(await jobs.list(1)) == 1
    await feed(data="jr:open:" + recent["nonce"])
    assert await ui.get(1, "input") is None
    await feed(data="jc:" + job.id)
    assert not any(
        b.callback_data == "retry:" + job.id
        for row in transport.calls[-1].reply_markup.inline_keyboard
        for b in row
    )
    await feed(data="jr:again:" + recent["nonce"])
    draft = await ui.get(1, "draft")
    assert draft["recent_bypass"] and draft["id"] != "source"
    await feed(data="cfg:run")
    new = next(j for j in await jobs.list(1) if j.id != job.id)
    assert new.state == "QUEUED"
    await jobs.cancel(1, new.id)
    # Batch selection creates a workspace and saves the selected cards without losing the selection.
    cards = await jobs.cards(1, job.id)
    await feed(data="jb:start:" + job.id)
    for card in cards:
        await feed(data=f"jb:t:{job.id}:{card['key'][:12]}:0")
    await feed(data="jb:pick:" + job.id)
    pending = await ui.get(1, "batch_pending")
    await feed(data="jb:new:" + pending["nonce"])
    await feed("Selected checks")
    folder = (await ui.extras.folders.list(1))[0]
    assert len(await ui.extras.folders.items(1, folder.id)) == 2
    # A repeated confirmation is idempotent at the database boundary.
    await feed(data=f"jb:save:{folder.id}:{pending['nonce']}")
    assert len(await ui.extras.folders.items(1, folder.id)) == 2
    first = cards[0]
    await feed(data=f"m:{job.id}:{first['key'][:12]}:f")
    assert (await jobs.cards(1, job.id))[0]["mark"] is None
    await feed(data=f"jf:{job.id}:{first['key'][:12]}:g")
    reviewed = (await jobs.cards(1, job.id))[0]
    assert reviewed["false_positive_reason"] == "generic_page" and reviewed["reviewed_at"]
    assert reviewed["observations"] == first["observations"]
    await feed(data=f"jf:{job.id}:{first['key'][:12]}:u", owner=2)
    assert (await jobs.cards(1, job.id))[0]["false_positive_reason"] == "generic_page"
    await feed(data="jg:file")
    assert (await ui.get(1, "input"))["type"] == "goal_file"
    await feed("/cancel")
    await jobs.settings(1, language="en")
    await feed("/start")
    assert any(
        "Check my website" == b.text
        for c in transport.calls
        if getattr(c, "reply_markup", None)
        for r in c.reply_markup.inline_keyboard
        for b in r
    )


async def test_explicit_site_chain_and_module_recent(client, jobs):
    ui, _, feed = client
    await feed("/start")
    await feed(data="jg:site")
    await feed("https://example.org/article")
    state = await ui.get(1, "site_choice")
    assert await jobs.list(1) == []
    await feed(data="jg:tool:passport:" + state["nonce"])
    draft = await ui.get(1, "tool_draft")
    assert draft["module"] == "passport" and await jobs.list(1) == []
    await feed(data="xt:run:" + draft["nonce"])
    job = await jobs.claim()
    data = {"final_url": "https://example.org/article"}
    await jobs.save_result(job.id, job.lease, {"schema_version": "echo.module/1", "data": data}, {})
    await jobs.finish(job.id, job.lease, {})
    # Passport to page is a draft, never an automatic fetch.
    await feed(data=f"jn:{job.id}:p")
    page_draft = await ui.get(1, "tool_draft")
    assert page_draft["module"] == "page" and len(await jobs.list(1)) == 1
    await feed(data="jn:" + job.id + ":p", owner=2)
    assert await ui.get(2, "tool_draft") is None
    # The same module input offers retained results, then requires confirmation to rerun.
    await feed("/tools")
    await feed(data="xt:choose:passport")
    await feed("https://example.org/article")
    recent = await ui.get(1, "recent")
    assert recent["recent_job"] == job.id
    await feed(data="jr:again:" + recent["nonce"])
    repeat = await ui.get(1, "tool_draft")
    assert repeat["module"] == "passport" and repeat["nonce"] != draft["nonce"]
    assert len(await jobs.list(1)) == 1
    # A QR result can only supply its own HTTP(S) URL; command and private targets are rejected.
    qr = await jobs.create(1, "", [], "tool", False, "qr-chain", kind="module", module_id="qr")
    leased = await jobs.claim()
    await jobs.save_result(
        qr.id,
        leased.lease,
        {
            "data": {
                "codes": [
                    {"url": "https://example.net/a"},
                    {"url": None, "text": "javascript:alert(1)"},
                    {"url": "http://169.254.169.254/latest"},
                ]
            }
        },
        {},
    )
    await jobs.finish(qr.id, leased.lease, {})
    await feed(data=f"jn:{qr.id}:q:0")
    qr_draft = await ui.get(1, "tool_draft")
    assert qr_draft["parameters"]["target"] == "https://example.net/a"
    await feed(data=f"jn:{qr.id}:q:1")
    await feed(data=f"jn:{qr.id}:q:2")
    assert (await ui.get(1, "tool_draft"))["nonce"] == qr_draft["nonce"]
    assert len(await jobs.list(1)) == 2


async def test_bulk_atomicity_review_exports_and_recent_bounds(client, jobs, observation, config, sessions):
    ui, _, _ = client
    job = await search_job(jobs, observation)
    cards = await jobs.cards(1, job.id)
    keys = [c["key"] for c in cards]
    folder = await ui.extras.folders.create(1, "My checks")
    config.max_folder_items = 1
    with pytest.raises(Limited):
        await ui.extras.folders.add_cards(1, folder.id, job.id, keys, "ru", "quota")
    assert await ui.extras.folders.items(1, folder.id) == []
    config.max_folder_items = 50
    await jobs.user(2)
    with pytest.raises(Denied):
        await ui.extras.folders.add_cards(2, folder.id, job.id, keys, "ru", "foreign")
    with pytest.raises(Denied):
        await ui.extras.folders.add_cards(1, folder.id, job.id, ["f" * 64], "ru", "invalid")
    await jobs.annotate(
        1, job.id, keys[0], mark="false_positive", reason="different_username", note="My assessment"
    )
    reports = Reports(jobs, config)
    snapshot = await reports.snapshot(1, job.id, "ru")
    coverage = snapshot["coverage_summary"]
    assert coverage["checks"] == 2 and coverage["blocked"] == 1 and coverage["not_checked_known"] == 2
    assert coverage["personal_false_positives"] == 1 and coverage["matches"] == 1
    assert snapshot["results"][0]["observations"][0]["status"] == Status.POSSIBLE_MATCH
    rendered = {fmt: await reports.build(1, job.id, fmt) for fmt in ("json", "html", "csv", "pdf")}
    assert json.loads(rendered["json"][0])["results"][0]["false_positive_reason"] == "different_username"
    html = rendered["html"][0].decode()
    assert 'data-personal="false_positive"' in html and "На странице другой ник" in html
    row = next(csv.DictReader(io.StringIO(rendered["csv"][0].decode("utf-8-sig"))))
    assert row["personal_review_reason"] == "different_username" and row["status"] == Status.POSSIBLE_MATCH
    assert "My assessment" in "".join(
        p.extract_text() for p in PdfReader(io.BytesIO(rendered["pdf"][0])).pages
    )
    if os.getenv("ECHO_TEST_DATABASE_URL"):
        await asyncio.gather(
            *(ui.extras.folders.add_cards(1, folder.id, job.id, keys, "ru", "same") for _ in range(2))
        )
    else:
        await ui.extras.folders.add_cards(1, folder.id, job.id, keys, "ru", "same")
        await ui.extras.folders.add_cards(1, folder.id, job.id, keys, "ru", "same")
    assert len(await ui.extras.folders.items(1, folder.id)) == 2
    assert (
        await jobs.recent(2, kind="search", nick=job.username, engines=job.engines, coverage=job.coverage)
        is None
    )
    assert (
        await jobs.recent(1, kind="search", nick=job.username, engines=["sherlock"], coverage=job.coverage)
        is None
    )
    async with sessions.begin() as s:
        stored = await s.get(Job, job.id)
        stored.finished_at = now() - timedelta(minutes=config.recent_result_minutes + 1)
    assert (
        await jobs.recent(1, kind="search", nick=job.username, engines=job.engines, coverage=job.coverage)
        is None
    )
    await jobs.annotate(1, job.id, keys[0], mark="clear")
    assert (await jobs.cards(1, job.id))[0]["false_positive_reason"] is None
    await jobs.delete_job(1, job.id)
    assert len(await ui.extras.folders.items(1, folder.id)) == 2


async def test_unknown_coverage_is_not_zero_remaining(jobs, observation):
    job = await search_job(jobs, observation)
    job.progress = {}
    result = search_coverage(job, await jobs.cards(1, job.id))
    assert not result["coverage_known"]
    assert result["engines"]["sherlock"]["selected"] is None
