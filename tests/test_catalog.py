import asyncio
import json
from datetime import timedelta

import httpx
from pydantic import SecretStr
from sqlalchemy import select

from echo.ai import Gateway
from echo.catalog import Catalog, seed_catalog
from echo.domain import now
from echo.storage import Site


async def test_reviewed_cache_never_calls_ai(sessions, redis, config):
    await seed_catalog(sessions)
    config.ai_enabled = True
    config.ai_key = SecretStr("not_a_real_key")

    def external(request):
        raise AssertionError("manual catalogue must not call AI")

    g = Gateway(config, redis, sessions, httpx.AsyncClient(transport=httpx.MockTransport(external)))
    c = Catalog(sessions, redis, g, config)
    for language in ("ru", "en"):
        entry = await c.get("github", language, "1", refresh=True)
        assert entry.origin == "human" and entry.description
    await g.close()


async def test_translation_singleflight_and_provenance(sessions, redis, config):
    await seed_catalog(sessions)
    async with sessions.begin() as s:
        site = await s.get(Site, ("github", "en"))
        await s.delete(site)
    config.ai_enabled = True
    config.ai_key = SecretStr("not_a_real_key")
    calls = []

    async def external(request):
        calls.append(request)
        await asyncio.sleep(0.05)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "site_name": "GitHub",
                                    "category": "development",
                                    "description": "A software collaboration platform.",
                                    "typical_activities": ["Hosting code"],
                                    "insufficient_evidence": False,
                                }
                            )
                        },
                    }
                ]
            },
        )

    g = Gateway(config, redis, sessions, httpx.AsyncClient(transport=httpx.MockTransport(external)))
    c = Catalog(sessions, redis, g, config)
    await asyncio.gather(*[c.get("github", "en", str(i)) for i in range(8)])
    assert len(calls) == 1
    cached = await c.get("github", "en", "10")
    assert len(calls) == 1 and cached.origin == "ai_translation:human"
    assert cached.sources == ["https://github.com/about"]
    assert (await c.get("github", "ru", "10")).origin == "human"
    await g.close()


async def test_negative_cache_and_stale_preservation(sessions, redis, config, monkeypatch):
    async with sessions.begin() as s:
        s.add(
            Site(
                id="example",
                language="en",
                name="Example",
                domains=["example.com"],
                category="other",
                description="Previously valid description.",
                origin="ai_source",
                reviewed="source_based",
                sources=["https://example.com/about"],
                refresh_at=now() - timedelta(days=1),
            )
        )
    calls = []

    async def unavailable(*args):
        calls.append(args)
        raise ValueError("unavailable")

    monkeypatch.setattr("echo.catalog.fetch_official", unavailable)
    g = Gateway(config, redis, sessions)
    c = Catalog(sessions, redis, g, config)
    entry = await c.get("example", "en", "1")
    assert entry.description == "Previously valid description."
    await c.get("example", "en", "2")
    assert len(calls) == 1
    await g.close()


async def test_unknown_domain_does_not_fetch_profile(sessions, redis, config, monkeypatch):
    async def forbidden(*args):
        raise AssertionError("unapproved source fetched")

    monkeypatch.setattr("echo.catalog.fetch_official", forbidden)
    g = Gateway(config, redis, sessions)
    c = Catalog(sessions, redis, g, config)
    assert await c.get("unapproved.example", "ru", "1") is None
    async with sessions() as s:
        assert list(await s.scalars(select(Site))) == []
    await g.close()


async def test_insufficient_evidence_negative_cache(sessions, redis, config, monkeypatch):
    async with sessions.begin() as s:
        s.add(
            Site(
                id="example",
                language="en",
                name="Example",
                domains=["example.com"],
                category="other",
                description="",
                origin="unavailable",
                sources=["https://example.com/about"],
                refresh_at=now() - timedelta(days=1),
            )
        )

    async def source(*args):
        return (
            "Official source has insufficient details to describe any capabilities.",
            "https://example.com/about",
        )

    monkeypatch.setattr("echo.catalog.fetch_official", source)
    config.ai_enabled = True
    config.ai_key = SecretStr("not_real")
    calls = []

    def external(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "site_name": "Example",
                                    "category": "other",
                                    "description": "",
                                    "typical_activities": [],
                                    "insufficient_evidence": True,
                                }
                            )
                        },
                    }
                ]
            },
        )

    g = Gateway(config, redis, sessions, httpx.AsyncClient(transport=httpx.MockTransport(external)))
    c = Catalog(sessions, redis, g, config)
    first = await c.get("example", "en", "1")
    assert first.origin == "unavailable" and first.description == ""
    await c.get("example", "en", "2")
    assert len(calls) == 1
    await g.close()
