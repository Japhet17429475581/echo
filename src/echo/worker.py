import asyncio
import contextlib
import json
import logging
import shutil
import uuid

from echo.catalog import identity
from echo.domain import Engine
from echo.engines import Adapter, DomainLimiter, EngineError
from echo.modules import ModuleError
from echo.resources import HeavySlots
from echo.services import Denied, StorageLimited
from echo.storage import Job, User
from echo.tasks import Tasks

log = logging.getLogger("echo")


class Worker:
    def __init__(self, config, jobs, redis, catalog):
        self.config, self.jobs, self.redis, self.catalog = config, jobs, redis, catalog
        self.limiter = DomainLimiter(redis, config.domain_interval)
        self.id = str(uuid.uuid4())
        self.tasks = Tasks(config, jobs, redis, self.limiter)
        self.slots = HeavySlots(redis, config)
        self.capabilities = {"available": {}}

    async def execute(self, job):
        async def operation():
            async with self.slots.hold():
                if job.kind == "search":
                    await self.execute_search(job)
                else:
                    await self.tasks.execute(job)
                    await self.jobs.finish(job.id, job.lease, {})

        async def heartbeat():
            while True:
                if not await self.jobs.heartbeat(job.id, job.lease):
                    return
                await asyncio.sleep(min(5, self.config.lease_seconds / 3))

        task = asyncio.create_task(operation())
        monitor = asyncio.create_task(heartbeat())
        try:
            done, _ = await asyncio.wait([task, monitor], return_when=asyncio.FIRST_COMPLETED)
            if monitor in done and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await self.jobs.finish(job.id, job.lease, {"worker": "cancelled_or_deleted"})
            else:
                await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            await self.jobs.finish(job.id, job.lease, {"worker": "resource_lease_lost"})
        except StorageLimited:
            await self.jobs.finish(job.id, job.lease, {"module": "storage_full"})
        except (ModuleError, ValueError, TimeoutError) as error:
            code = (
                str(error)
                if isinstance(error, ModuleError)
                else "tool_timeout"
                if isinstance(error, TimeoutError)
                else "invalid_input"
            )
            await self.jobs.finish(job.id, job.lease, {"module": code})
        except Exception:
            log.warning("task_failed")
            await self.jobs.finish(job.id, job.lease, {"module": "task_failed"})
        finally:
            task.cancel()
            monitor.cancel()
            await asyncio.gather(task, monitor, return_exceptions=True)

    async def execute_search(self, job):
        stop = asyncio.Event()
        failures = {}
        aliases = await self.catalog.aliases()

        async def monitor():
            while True:
                if not await self.jobs.heartbeat(job.id, job.lease):
                    stop.set()
                    return
                await asyncio.sleep(min(5, self.config.lease_seconds / 3))

        async def engine_run(engine):
            if await self.redis.exists(f"echo:disabled:{engine}"):
                failures[engine] = "disabled"
                return

            async def save(observation):
                site_id = identity(observation.domain, observation.site_key, aliases)
                if not await self.jobs.save(job.id, job.lease, observation, site_id):
                    stop.set()

            async def progress(info):
                await self.jobs.progress(job.id, job.lease, engine, info)

            try:
                skip = await self.jobs.saved_sites(job.id, engine)
                await Adapter(Engine(engine), self.config, self.limiter).run(job, skip, save, progress)
            except EngineError as e:
                failures[engine] = e.code
            except asyncio.CancelledError:
                raise
            except Exception:
                failures[engine] = "adapter_error"
                log.warning("engine_adapter_failed engine=%s", engine)

        monitor_task = asyncio.create_task(monitor())

        async def run_engines():
            if self.config.sequential_engines:
                for engine in job.engines:
                    await engine_run(engine)
            else:
                await asyncio.gather(*(engine_run(e) for e in job.engines))

        combined = asyncio.create_task(run_engines())
        cancelled = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait([combined, cancelled], return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                combined.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await combined
                failures["worker"] = "cancelled_or_deleted"
            else:
                await combined
            await self.jobs.finish(job.id, job.lease, failures)
        finally:
            monitor_task.cancel()
            cancelled.cancel()
            combined.cancel()
            await asyncio.gather(monitor_task, cancelled, combined, return_exceptions=True)

    async def searches(self):
        while True:
            try:
                job = await self.jobs.claim()
                if job:
                    await self.execute(job)
                else:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("queue_iteration_failed")
                await asyncio.sleep(3)

    async def enrichment(self):
        while True:
            try:
                job = await self.jobs.enrich_claim()
                if not job:
                    await asyncio.sleep(2)
                    continue
                async with self.jobs.sessions() as s:
                    u = await s.get(User, job.owner_id)
                if not u or not u.ai:
                    await self.jobs.enrich_state(job.id, job.generation, "DISABLED", job.lease)
                    continue
                cards = await self.jobs.cards(job.owner_id, job.id, u.language)
                seen = set()
                state = "COMPLETED"

                async def active(job=job):
                    try:
                        latest = await self.jobs.owned(job.owner_id, job.id)
                    except Denied:
                        return False
                    async with self.jobs.sessions() as session:
                        owner = await session.get(User, job.owner_id)
                    return bool(
                        not latest.cancelled
                        and latest.lease == job.lease
                        and owner
                        and owner.ai
                        and owner.generation == job.generation
                    )

                for card in cards:
                    fresh = await self.jobs.owned(job.owner_id, job.id)
                    async with self.jobs.sessions() as s:
                        current_user = await s.get(User, job.owner_id)
                    if (
                        fresh.cancelled
                        or not current_user
                        or not current_user.ai
                        or current_user.generation != job.generation
                    ):
                        state = "CANCELLED"
                        break
                    if card["site_id"] in seen:
                        continue
                    seen.add(card["site_id"])
                    if not await self.jobs.enrichment_heartbeat(job.id, job.lease):
                        break
                    refresh = asyncio.create_task(
                        self.catalog.get(card["site_id"], u.language, u.generation, guard=active)
                    )
                    try:
                        while not refresh.done():
                            await asyncio.wait([refresh], timeout=5)
                            if not await self.jobs.enrichment_heartbeat(job.id, job.lease):
                                refresh.cancel()
                                state = "CANCELLED"
                                break
                        if state == "CANCELLED":
                            break
                        result = await refresh
                    finally:
                        refresh.cancel()
                        await asyncio.gather(refresh, return_exceptions=True)
                    if not result or not result.description:
                        state = "PARTIAL"
                await self.jobs.enrich_state(job.id, job.generation, state, job.lease)
            except Denied:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("enrichment_iteration_failed")
                await asyncio.sleep(3)

    async def housekeeping(self):
        while True:
            try:
                await self.redis.set("echo:worker:" + self.id, "alive", ex=30)
                await self.redis.set("echo:tools:" + self.id, json.dumps(self.capabilities), ex=30)
                await self.jobs.cleanup()
                await self.tasks.vault.janitor()
                # Crashed-process leftovers contain no exports; each child gets a private directory.
                root = self.config.temp_dir.resolve()
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                for path in [*root.glob("engine-*"), *root.glob("module-*")]:
                    resolved = path.resolve()
                    marker = resolved / "lease.json"
                    if resolved.parent != root or path.is_symlink() or not marker.is_file():
                        continue
                    try:
                        record = json.loads(await asyncio.to_thread(marker.read_text, "utf-8"))
                        async with self.jobs.sessions() as s:
                            active = await s.get(Job, record["job"])
                        if active and active.lease == record["lease"]:
                            continue
                        await asyncio.to_thread(shutil.rmtree, resolved)
                    except (OSError, ValueError, KeyError):
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("cleanup_failed")
            await asyncio.sleep(min(10, self.config.cleanup_interval))

    async def run(self):
        self.capabilities = await self.tasks.modules.runner.probe()
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self.searches())
                group.create_task(self.enrichment())
                group.create_task(self.housekeeping())
        finally:
            with contextlib.suppress(Exception):
                await self.redis.delete("echo:worker:" + self.id)
                await self.redis.delete("echo:tools:" + self.id)
