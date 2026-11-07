import asyncio
import os
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from echo.domain import Engine, JobState, Status, now
from echo.services import Denied, Limited
from echo.storage import Annotation, Check, Job, User


async def create(jobs, key="request1", engines=None):
    await jobs.user(1)
    return await jobs.create(
        1, "echo_fixture_user", engines or ["sherlock", "maigret"], "curated", False, key
    )


async def test_idempotency_and_ownership(jobs):
    a, b = await create(jobs), await create(jobs)
    assert a.id == b.id
    await jobs.user(2)
    with pytest.raises(Denied):
        await jobs.create(2, "x", ["sherlock"], "curated", False, "request1")
    for operation in [
        jobs.owned(2, a.id),
        jobs.cancel(2, a.id),
        jobs.cards(2, a.id),
        jobs.delete_job(2, a.id),
    ]:
        with pytest.raises(Denied):
            await operation


async def test_cancel_queued_and_running(jobs, observation):
    j = await create(jobs)
    await jobs.cancel(1, j.id)
    assert await jobs.claim() is None
    assert (await jobs.owned(1, j.id)).state == JobState.CANCELLED
    j = await create(jobs, "r2")
    j = await jobs.claim()
    assert await jobs.save(j.id, j.lease, observation, "github")
    await jobs.cancel(1, j.id)
    assert not await jobs.save(j.id, j.lease, observation, "github")
    assert not await jobs.heartbeat(j.id, j.lease)
    await jobs.finish(j.id, j.lease, {})
    assert (await jobs.owned(1, j.id)).state == JobState.CANCELLED


async def test_lease_recovery_and_fencing(jobs, sessions, observation):
    await create(jobs)
    first = await jobs.claim()
    await jobs.save(first.id, first.lease, observation, "github")
    async with sessions.begin() as s:
        j = await s.get(Job, first.id)
        j.lease_until = now() - timedelta(seconds=1)
    second = await jobs.claim()
    assert second.id == first.id and second.lease != first.lease
    assert not await jobs.save(first.id, first.lease, observation, "github")
    assert await jobs.saved_sites(second.id, "sherlock") == ["GitHub"]
    await jobs.save(second.id, second.lease, observation, "github")
    assert len(await jobs.cards(1, second.id)) == 1
    await jobs.finish(second.id, second.lease, {"maigret": "timeout"})
    assert (await jobs.owned(1, second.id)).state == JobState.PARTIAL


async def test_delete_prevents_resurrection(jobs, sessions, observation):
    await create(jobs)
    j = await jobs.claim()
    await jobs.save(j.id, j.lease, observation, "github")
    cards = await jobs.cards(1, j.id)
    await jobs.annotate(1, j.id, cards[0]["key"], mark="useful", note="private note")
    await jobs.delete_user(1)
    assert not await jobs.save(j.id, j.lease, observation, "github")
    await jobs.finish(j.id, j.lease, {})
    for model in (User, Job, Check, Annotation):
        async with sessions() as s:
            assert await s.scalar(select(func.count()).select_from(model)) == 0
    recreated = await jobs.user(1)
    assert recreated.generation != j.generation
    assert not await jobs.heartbeat(j.id, j.lease)


async def test_ttl_history_and_cleanup(jobs, sessions, observation):
    await create(jobs)
    j = await jobs.claim()
    assert not (await jobs.user(1)).history
    await jobs.save(j.id, j.lease, observation, "github")
    await jobs.settings(1, history=True)
    assert (await jobs.owned(1, j.id)).expires_at > j.expires_at
    async with sessions.begin() as s:
        stored = await s.get(Job, j.id)
        stored.expires_at = now() - timedelta(seconds=1)
    assert not await jobs.save(j.id, j.lease, observation, "github")
    await jobs.cleanup()
    with pytest.raises(Denied):
        await jobs.owned(1, j.id)
    async with sessions() as s:
        active = await s.get(Job, j.id)
        assert active is not None and active.cancelled
    await jobs.finish(j.id, j.lease, {})
    await jobs.cleanup()
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Check)) == 0


async def test_merge_keeps_both_engine_facts_and_annotations(jobs, observation):
    await create(jobs)
    j = await jobs.claim()
    await jobs.save(j.id, j.lease, observation, "github")
    second = observation.model_copy(
        update={"engine": Engine.MAIGRET, "version": "0.6.6", "status": Status.NOT_FOUND}
    )
    await jobs.save(j.id, j.lease, second, "github")
    cards = await jobs.cards(1, j.id)
    assert len(cards) == 1 and len(cards[0]["observations"]) == 2
    with pytest.raises(Denied):
        await jobs.annotate(2, j.id, cards[0]["key"], note="not mine")
    await jobs.annotate(1, j.id, cards[0]["key"], mark="false_positive", note="reviewed")
    assert (await jobs.cards(1, j.id))[0]["note"] == "reviewed"


async def test_retry_is_child_and_does_not_replace_original(jobs, observation):
    await create(jobs)
    j = await jobs.claim()
    error = observation.model_copy(update={"status": Status.TIMEOUT, "raw_status": "Unknown"})
    await jobs.save(j.id, j.lease, error, "github")
    await jobs.finish(j.id, j.lease, {})
    child = await jobs.retry(1, j.id, "retry1")
    assert child.parent_id == j.id and child.targets == {"sherlock": ["GitHub"]}
    assert len(await jobs.cards(1, j.id)) == 1
    assert (await jobs.retry(1, j.id, "retry1")).id == child.id


async def test_queue_limit(jobs, config):
    config.user_active_jobs = 1
    await create(jobs)
    with pytest.raises(Limited):
        await create(jobs, "other")


async def test_retry_honors_retry_after(jobs, observation):
    await create(jobs)
    j = await jobs.claim()
    error = observation.model_copy(
        update={"status": Status.RATE_LIMITED, "http_code": 429, "retry_after": "120", "checked_at": now()}
    )
    await jobs.save(j.id, j.lease, error, "github")
    await jobs.finish(j.id, j.lease, {})
    child = await jobs.retry(1, j.id, "later")
    assert child.not_before >= now() + timedelta(seconds=118)
    assert await jobs.claim() is None


async def test_progress_after_recovery_counts_saved_matches(jobs, observation):
    await create(jobs)
    j = await jobs.claim()
    await jobs.save(j.id, j.lease, observation, "github")
    await jobs.progress(j.id, j.lease, "sherlock", {"completed": 1, "matches": 0, "errors": 0})
    assert (await jobs.owned(1, j.id)).progress["sherlock"]["matches"] == 1


async def test_enrichment_lease_fences_stale_worker(jobs, sessions):
    await jobs.user(1)
    await jobs.settings(1, ai=True)
    await jobs.create(1, "echo_fixture_user", ["sherlock"], "curated", True, "enrichment")
    search = await jobs.claim()
    await jobs.finish(search.id, search.lease, {})
    first = await jobs.enrich_claim()
    async with sessions.begin() as s:
        job = await s.get(Job, first.id)
        job.lease_until = now() - timedelta(seconds=1)
    second = await jobs.enrich_claim()
    assert first.lease != second.lease
    await jobs.enrich_state(first.id, first.generation, "COMPLETED", first.lease)
    current = await jobs.owned(1, first.id)
    assert current.enrichment == "RUNNING" and current.lease == second.lease
    await jobs.enrich_state(second.id, second.generation, "COMPLETED", second.lease)
    assert (await jobs.owned(1, second.id)).enrichment == "COMPLETED"


@pytest.mark.skipif(not os.getenv("ECHO_TEST_DATABASE_URL"), reason="PostgreSQL locking required")
async def test_concurrent_submit_and_claim(jobs, config):
    await jobs.user(1)
    submissions = await asyncio.gather(
        *[jobs.create(1, "echo_fixture_user", ["sherlock"], "curated", False, "same_key") for _ in range(8)]
    )
    assert len({j.id for j in submissions}) == 1
    claimed = await asyncio.gather(*[jobs.claim() for _ in range(5)])
    assert len([j for j in claimed if j]) == 1


@pytest.mark.skipif(not os.getenv("ECHO_TEST_DATABASE_URL"), reason="PostgreSQL locking required")
async def test_concurrent_delete_and_write(jobs, observation):
    await create(jobs)
    j = await jobs.claim()
    await asyncio.gather(jobs.save(j.id, j.lease, observation, "github"), jobs.delete_user(1))
    assert not await jobs.save(j.id, j.lease, observation, "github")
    with pytest.raises(Denied):
        await jobs.owned(1, j.id)
