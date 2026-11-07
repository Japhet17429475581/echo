import asyncio
import json
import shutil
from datetime import UTC, datetime

from sqlalchemy import func, select, text

from echo.storage import AICall, Check, FileObject, Job, Site


class Admin:
    def __init__(self, sessions, redis, config):
        self.sessions, self.redis, self.config = sessions, redis, config

    async def catalog_count(self):
        async with self.sessions() as s:
            return await s.scalar(select(func.count()).select_from(Site))

    async def snapshot(self):
        async with self.sessions() as s:
            await s.execute(text("SELECT 1"))
            states = dict((await s.execute(select(Job.state, func.count()).group_by(Job.state))).all())
            checks = [
                {"engine": e, "status": status, "count": n}
                for e, status, n in (
                    await s.execute(
                        select(Check.engine, Check.status, func.count()).group_by(Check.engine, Check.status)
                    )
                ).all()
            ]
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            ai = (
                await s.execute(
                    select(
                        func.count(AICall.id),
                        func.sum(AICall.attempts),
                        func.sum(AICall.input_tokens),
                        func.sum(AICall.output_tokens),
                        func.sum(AICall.elapsed_ms),
                    ).where(AICall.day == day)
                )
            ).one()
            outcomes = dict(
                (
                    await s.execute(
                        select(AICall.state, func.count()).where(AICall.day == day).group_by(AICall.state)
                    )
                ).all()
            )
            modules = {}
            rows = await s.stream(select(Job.kind, Job.module_id, Job.state, Job.failures, Job.metrics))
            async for kind, module, state, failures, metrics in rows:
                group = modules.setdefault(module or kind, {"states": {}, "timeouts": 0, "limits": 0})
                group["states"][state] = group["states"].get(state, 0) + 1
                codes = set((failures or {}).values())
                group["timeouts"] += int(bool(codes & {"tool_timeout", "timeout"}))
                group["limits"] += int(bool(codes & {"resource_limit", "output_limit", "storage_full"}))
                for key in (
                    "duration_seconds",
                    "queue_wait_seconds",
                    "input_bytes",
                    "result_bytes",
                    "cpu_tree_seconds_sampled",
                ):
                    value = (metrics or {}).get(key)
                    if isinstance(value, (int, float)):
                        group[key + "_total"] = group.get(key + "_total", 0) + value
                peak = (metrics or {}).get("peak_tree_rss_bytes_sampled", 0)
                group["peak_tree_rss_bytes_sampled"] = max(group.get("peak_tree_rss_bytes_sampled", 0), peak)
            file_bytes = await s.scalar(select(func.coalesce(func.sum(FileObject.size), 0)))
        hit = int(await self.redis.get("echo:ai:cache_hit") or 0)
        miss = int(await self.redis.get("echo:ai:cache_miss") or 0)
        budget = await self.redis.hgetall(f"echo:ai:budget:{day}")
        reserved_usd = int(budget.get("micros", 0)) / 1_000_000
        workers = sum([1 async for _ in self.redis.scan_iter(match="echo:worker:*")])
        available = set()
        async for key in self.redis.scan_iter(match="echo:tools:*"):
            raw = await self.redis.get(key)
            if raw:
                available.update(
                    k
                    for k, v in json.loads(raw).get("available", {}).items()
                    if v and getattr(self.config, k + "_enabled", False)
                )
        try:
            free = (await asyncio.to_thread(shutil.disk_usage, self.config.data_dir)).free
        except OSError:
            free = None
        return {
            "postgres": "ok",
            "redis": "ok" if await self.redis.ping() else "failed",
            "workers": workers,
            "queue": states,
            "engine_metrics": checks,
            "task_metrics_retained": modules,
            "memory_measurement": "max of sampled process-tree sum RSS; 100ms; excludes server stack",
            "stored_or_reserved_bytes": int(file_bytes),
            "data_disk_free_bytes": free,
            "resource_profile": self.config.resource_profile,
            "heavy_slots_limit": self.config.heavy_slots,
            "available_modules": sorted(available),
            "ai_calls": ai[0],
            "external_attempts": int(ai[1] or 0),
            "input_tokens_known": int(ai[2]) if ai[2] is not None else None,
            "output_tokens_known": int(ai[3]) if ai[3] is not None else None,
            "elapsed_ms": int(ai[4]) if ai[4] is not None else None,
            "ai_outcomes": outcomes,
            "cache_hit_rate": hit / (hit + miss) if hit + miss else None,
            "reserved_or_spent_usd": reserved_usd if self.config.ai_price_date else None,
            "remaining_usd": max(0, self.config.ai_daily_budget_usd - reserved_usd)
            if self.config.ai_price_date
            else None,
            "request_reservations": int(budget.get("requests", 0)),
            "circuit_breaker": await self.redis.get("echo:ai:breaker"),
            "disabled": [
                f for f in ("ai", "sherlock", "maigret") if await self.redis.exists(f"echo:disabled:{f}")
            ],
        }
