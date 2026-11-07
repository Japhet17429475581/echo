from __future__ import annotations

import hashlib
import json
import random
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, update

from echo.config import Settings
from echo.domain import (
    ACTIVE,
    FALSE_POSITIVE_REASONS,
    TRANSIENT,
    JobState,
    Observation,
    now,
    safe_url,
    username,
)
from echo.retry import retry_after
from echo.storage import Annotation, Check, FileObject, Gate, Job, Site, User, Workspace


class Denied(Exception):
    pass


class Limited(Exception):
    pass


class StorageLimited(Limited):
    pass


class FolderLimited(Limited):
    pass


class Jobs:
    """Database is the durable queue. A lease token fences every worker write."""

    def __init__(self, sessions, config: Settings):
        self.sessions, self.config = sessions, config

    async def user(self, owner: int) -> User:
        async with self.sessions.begin() as s:
            # Serializes creation/settings/deletion with queue submissions.
            await s.get(Gate, 1, with_for_update=True)
            u = await s.get(User, owner)
            if not u:
                u = User(id=owner)
                s.add(u)
                await s.flush()
            return u

    async def settings(self, owner: int, **values):
        allowed = {"language", "history", "ai", "recipient"}
        if values.keys() - allowed:
            raise ValueError("invalid_setting")
        if "language" in values and values["language"] not in {"ru", "en"}:
            raise ValueError("invalid_language")
        async with self.sessions.begin() as s:
            u = await s.get(User, owner, with_for_update=True)
            if not u:
                raise Denied()
            for k, v in values.items():
                setattr(u, k, v)
            if "history" in values:
                ttl = (
                    timedelta(days=self.config.history_ttl_days)
                    if values["history"]
                    else timedelta(hours=self.config.result_ttl_hours)
                )
                for job in await s.scalars(select(Job).where(Job.owner_id == owner)):
                    job.expires_at = now() + ttl
                    await s.execute(
                        update(FileObject)
                        .where(FileObject.job_id == job.id)
                        .values(expires_at=job.expires_at)
                    )

    async def create(
        self,
        owner: int,
        nick: str,
        engines: list[str],
        coverage: str,
        ai: bool,
        request_key: str,
        parent: str | None = None,
        targets: dict | None = None,
        not_before=None,
        kind="search",
        module_id=None,
        parameters=None,
    ) -> Job:
        nick = username(nick) if kind == "search" else nick[:64]
        if kind not in {"search", "module", "export", "folder_export", "compare"}:
            raise ValueError("invalid_kind")
        if kind == "search" and (not engines or set(engines) - {"sherlock", "maigret"}):
            raise ValueError("invalid_engines")
        if kind == "search" and coverage not in {"curated", "top100", "extended", "retry"}:
            raise ValueError("invalid_coverage")
        async with self.sessions.begin() as s:
            await s.get(Gate, 1, with_for_update=True)
            existing = await s.scalar(select(Job).where(Job.request_key == request_key))
            if existing:
                if existing.owner_id != owner:
                    raise Denied()
                return existing
            u = await s.get(User, owner, with_for_update=True)
            if not u:
                raise Denied()
            if parent:
                p = await s.get(Job, parent)
                if not p or p.owner_id != owner:
                    raise Denied()
            active = await s.scalar(
                select(func.count()).select_from(Job).where(Job.owner_id == owner, Job.state.in_(ACTIVE))
            )
            queued = await s.scalar(select(func.count()).select_from(Job).where(Job.state.in_(ACTIVE)))
            daily = await s.scalar(
                select(func.count())
                .select_from(Job)
                .where(
                    Job.owner_id == owner,
                    Job.created_at >= now().replace(hour=0, minute=0, second=0, microsecond=0),
                )
            )
            if (
                active >= self.config.user_active_jobs
                or queued >= self.config.global_queued_jobs
                or daily >= self.config.user_daily_jobs
            ):
                raise Limited()
            ttl = (
                timedelta(days=self.config.history_ttl_days)
                if u.history
                else timedelta(hours=self.config.result_ttl_hours)
            )
            j = Job(
                owner_id=owner,
                generation=u.generation,
                username=nick,
                engines=engines,
                coverage=coverage,
                ai=ai and u.ai,
                request_key=request_key,
                parent_id=parent,
                targets=targets,
                not_before=not_before or now(),
                kind=kind,
                module_id=module_id,
                parameters=parameters or {},
                expires_at=now() + ttl,
                enrichment="PENDING" if ai and u.ai else "DISABLED",
            )
            s.add(j)
            await s.flush()
            return j

    async def owned(self, owner: int, job_id: str) -> Job:
        async with self.sessions() as s:
            j = await s.get(Job, job_id)
            if not j or j.owner_id != owner or as_utc(j.expires_at) <= now():
                raise Denied()
            return j

    async def recent(
        self, owner, *, kind, nick="", engines=(), coverage="", ai=False, module=None, parameters=None
    ):
        """Only this owner's retained, usable checks with the same explicit parameters."""
        async with self.sessions() as s:
            query = select(Job).where(
                Job.owner_id == owner,
                Job.kind == kind,
                Job.expires_at > now(),
                Job.finished_at >= now() - timedelta(minutes=self.config.recent_result_minutes),
                Job.state.in_([JobState.COMPLETED, JobState.PARTIAL]),
            )
            if kind == "search":
                query = query.where(Job.username == nick, Job.coverage == coverage, Job.ai == ai)
            elif kind == "module" and module in {"passport", "page", "domains"}:
                if not parameters or not parameters.get("target"):
                    return None
                query = query.where(Job.module_id == module)
                for key in ("target", "variants", "dns"):
                    if key in (parameters or {}):
                        value = parameters[key]
                        field = Job.parameters[key]
                        query = query.where(
                            (
                                field.as_boolean()
                                if isinstance(value, bool)
                                else field.as_integer()
                                if isinstance(value, int)
                                else field.as_string()
                            )
                            == value
                        )
            else:
                return None
            for job in await s.scalars(query.order_by(Job.finished_at.desc()).limit(100)):
                if kind == "search":
                    if set(job.engines) != set(engines):
                        continue
                    if not await s.scalar(select(Check.id).where(Check.job_id == job.id).limit(1)):
                        continue
                elif not job.result:
                    continue
                return job
        return None

    async def attach(self, owner: int, job_id: str, chat: int, message: int):
        async with self.sessions.begin() as s:
            j = await s.get(Job, job_id, with_for_update=True)
            if not j or j.owner_id != owner:
                raise Denied()
            j.chat_id, j.message_id = chat, message

    async def list(self, owner: int):
        async with self.sessions() as s:
            return list(
                await s.scalars(
                    select(Job)
                    .where(Job.owner_id == owner, Job.expires_at > now())
                    .order_by(Job.created_at.desc())
                    .limit(20)
                )
            )

    async def claim(self) -> Job | None:
        async with self.sessions.begin() as s:
            await s.get(Gate, 1, with_for_update=True)
            # Recover abandoned work without discarding saved intermediate checks.
            stale = await s.scalars(
                select(Job).where(Job.state == JobState.RUNNING, Job.lease_until < now()).with_for_update()
            )
            for j in stale:
                j.lease = None
                if j.cancelled:
                    j.state, j.finished_at = JobState.CANCELLED, now()
                elif j.attempt >= self.config.max_deliveries:
                    count = await s.scalar(
                        select(func.count()).select_from(Check).where(Check.job_id == j.id)
                    )
                    j.state = JobState.PARTIAL if count else JobState.FAILED
                    j.failures = {**j.failures, "worker": "lease_exhausted"}
                    j.finished_at = now()
                else:
                    j.state = JobState.QUEUED
            await s.flush()
            running = await s.scalar(
                select(func.count()).select_from(Job).where(Job.state == JobState.RUNNING)
            )
            if running >= self.config.global_active_jobs:
                return None
            j = await s.scalar(
                select(Job)
                .where(
                    Job.state == JobState.QUEUED,
                    Job.cancelled.is_(False),
                    Job.expires_at > now(),
                    Job.not_before <= now(),
                )
                .order_by(Job.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if not j:
                return None
            j.state, j.lease, j.lease_until = JobState.RUNNING, str(uuid.uuid4()), self.lease_deadline()
            j.started_at = j.started_at or now()
            j.attempt += 1
            await s.flush()
            return j

    def lease_deadline(self):
        return now() + timedelta(seconds=self.config.lease_seconds)

    async def heartbeat(self, job_id: str, token: str) -> bool:
        async with self.sessions.begin() as s:
            j = await s.get(Job, job_id, with_for_update=True)
            if not self.writable(j, token):
                return False
            j.lease_until = self.lease_deadline()
            return True

    @staticmethod
    def writable(j: Job | None, token: str):
        return bool(
            j
            and j.lease == token
            and j.state == JobState.RUNNING
            and not j.cancelled
            and as_utc(j.expires_at) > now()
            and j.lease_until
            and as_utc(j.lease_until) > now()
        )

    async def save(self, job_id: str, token: str, o: Observation, site_id: str):
        async with self.sessions.begin() as s:
            j = await s.get(Job, job_id, with_for_update=True)
            if not self.writable(j, token):
                return False
            u = await s.get(User, j.owner_id)
            if not u or u.generation != j.generation:
                return False
            key = hashlib.sha256(
                json.dumps([site_id, safe_url(o.url)], ensure_ascii=False).encode()
            ).hexdigest()
            check = await s.scalar(
                select(Check).where(
                    Check.job_id == job_id, Check.engine == o.engine, Check.site_key == o.site_key
                )
            )
            if not check:
                s.add(
                    Check(
                        job_id=job_id,
                        engine=o.engine,
                        site_key=o.site_key,
                        profile_key=key,
                        site_id=site_id,
                        status=o.status,
                        data=o.model_dump(mode="json"),
                    )
                )
            return True

    async def progress(self, job_id, token, engine, data):
        async with self.sessions.begin() as s:
            j = await s.get(Job, job_id, with_for_update=True)
            if self.writable(j, token):
                counts = dict(
                    (
                        await s.execute(
                            select(Check.status, func.count())
                            .where(Check.job_id == job_id, Check.engine == engine)
                            .group_by(Check.status)
                        )
                    ).all()
                )
                data = {
                    **data,
                    "matches": counts.get("POSSIBLE_MATCH", 0),
                    "errors": sum(
                        n for status, n in counts.items() if status not in {"POSSIBLE_MATCH", "NOT_FOUND"}
                    ),
                }
                j.progress = {**j.progress, engine: data}

    async def saved_sites(self, job_id: str, engine: str):
        async with self.sessions() as s:
            return list(
                await s.scalars(select(Check.site_key).where(Check.job_id == job_id, Check.engine == engine))
            )

    async def finish(self, job_id: str, token: str, failures: dict):
        async with self.sessions.begin() as s:
            j = await s.get(Job, job_id, with_for_update=True)
            if not j or j.lease != token:
                return
            count = await s.scalar(select(func.count()).select_from(Check).where(Check.job_id == job_id))
            if j.kind != "search":
                count = int(j.result is not None)
            j.state = (
                JobState.CANCELLED
                if j.cancelled
                else (JobState.PARTIAL if count else JobState.FAILED)
                if failures
                else JobState.COMPLETED
            )
            j.failures, j.finished_at, j.lease = failures, now(), None
            metrics = dict(j.metrics or {})
            if j.started_at:
                metrics.setdefault(
                    "duration_seconds", max(0, (j.finished_at - as_utc(j.started_at)).total_seconds())
                )
                metrics.setdefault(
                    "queue_wait_seconds",
                    max(0, (as_utc(j.started_at) - as_utc(j.created_at)).total_seconds()),
                )
            j.metrics = metrics
            if j.cancelled:
                j.enrichment = "CANCELLED"

    async def cancel(self, owner: int, job_id: str):
        async with self.sessions.begin() as s:
            j = await s.get(Job, job_id, with_for_update=True)
            if not j or j.owner_id != owner:
                raise Denied()
            j.cancelled = True
            if j.state == JobState.QUEUED:
                j.state, j.finished_at = JobState.CANCELLED, now()
            j.enrichment = "CANCELLED"

    async def cards(self, owner: int, job_id: str, language: str = "ru") -> list[dict]:
        await self.owned(owner, job_id)
        async with self.sessions() as s:
            checks = list(await s.scalars(select(Check).where(Check.job_id == job_id).order_by(Check.id)))
            annotations = {
                a.profile_key: a
                for a in await s.scalars(select(Annotation).where(Annotation.job_id == job_id))
            }
            sites = {site.id: site for site in await s.scalars(select(Site).where(Site.language == language))}
            merged = {}
            for c in checks:
                if c.profile_key not in merged:
                    a, site = annotations.get(c.profile_key), sites.get(c.site_id)
                    merged[c.profile_key] = {
                        "key": c.profile_key,
                        "site_id": c.site_id,
                        "name": c.data["site_name"],
                        "url": safe_url(c.data["url"]),
                        "username": c.data["username"],
                        "observations": [],
                        "mark": a.mark if a else None,
                        "note": a.note if a else "",
                        "false_positive_reason": a.false_positive_reason if a else None,
                        "reviewed_at": as_utc(a.reviewed_at).isoformat() if a and a.reviewed_at else None,
                        "site": site_dict(site) if site else None,
                    }
                merged[c.profile_key]["observations"].append(c.data)
            return list(merged.values())

    async def annotate(self, owner, job_id, key, *, mark=None, note=None, reason=None):
        if reason is not None and (reason not in FALSE_POSITIVE_REASONS or mark != "false_positive"):
            raise ValueError("invalid_review_reason")
        async with self.sessions.begin() as s:
            j = await s.get(Job, job_id, with_for_update=True)
            if not j or j.owner_id != owner or as_utc(j.expires_at) <= now():
                raise Denied()
            exists = await s.scalar(select(Check.id).where(Check.job_id == job_id, Check.profile_key == key))
            if not exists:
                raise Denied()
            if mark not in {None, "useful", "false_positive", "clear"}:
                raise ValueError("invalid_mark")
            a = await s.get(Annotation, (job_id, key))
            if not a:
                a = Annotation(job_id=job_id, profile_key=key)
                s.add(a)
            if mark is not None:
                a.mark = None if mark == "clear" else mark
                a.false_positive_reason = reason if mark == "false_positive" else None
                a.reviewed_at = now() if mark == "false_positive" else None
            if note is not None:
                a.note = note[:1000]

    async def retry(self, owner, job_id, request_key):
        j = await self.owned(owner, job_id)
        if j.state in ACTIVE:
            raise Limited()
        async with self.sessions() as s:
            checks = await s.scalars(select(Check).where(Check.job_id == job_id, Check.status.in_(TRANSIENT)))
            targets = {}
            wait = 0.0
            for c in checks:
                targets.setdefault(c.engine, []).append(c.site_key)
                checked = as_utc(datetime.fromisoformat(c.data["checked_at"]))
                delay = retry_after(c.data.get("retry_after"), 3600, current=checked)
                if delay is not None:
                    wait = max(wait, (checked + timedelta(seconds=delay) - now()).total_seconds())
            depth, parent = 0, j
            while parent and parent.parent_id and depth < 6:
                depth += 1
                parent = await s.get(Job, parent.parent_id)
        if not targets:
            raise Limited()
        return await self.create(
            owner,
            j.username,
            list(targets),
            "retry",
            j.ai,
            request_key,
            parent=job_id,
            targets=targets,
            not_before=now() + timedelta(seconds=max(wait, min(30, 2**depth)) + random.uniform(0, 0.5)),
        )

    async def delete_job(self, owner, job_id, clear=None):
        async with self.sessions.begin() as s:
            await s.get(User, owner, with_for_update=True)
            j = await s.get(Job, job_id, with_for_update=True)
            if not j or j.owner_id != owner:
                raise Denied()
            await s.execute(
                delete(Job).where(
                    Job.owner_id == owner,
                    Job.kind == "export",
                    Job.parameters["source_job"].as_string() == job_id,
                )
            )
            await s.delete(j)
            if clear:
                await clear()

    async def delete_user(self, owner, clear=None):
        async with self.sessions.begin() as s:
            await s.get(Gate, 1, with_for_update=True)
            u = await s.get(User, owner, with_for_update=True)
            if u:
                await s.execute(delete(Job).where(Job.owner_id == owner))
                await s.delete(u)
            if clear:
                await clear()

    async def cleanup(self):
        async with self.sessions.begin() as s:
            expired = await s.scalars(
                select(Job).where(Job.expires_at <= now(), Job.state.in_(ACTIVE)).with_for_update()
            )
            for j in expired:
                j.cancelled = True
                if j.state == JobState.QUEUED:
                    j.state, j.finished_at = JobState.CANCELLED, now()
            await s.execute(
                delete(Job)
                .where(Job.expires_at <= now(), Job.state != JobState.RUNNING)
                .execution_options(synchronize_session=False)
            )
            await s.execute(delete(Workspace).where(Workspace.expires_at <= now()))
            await s.execute(
                delete(FileObject).where(
                    FileObject.expires_at <= now(),
                    FileObject.job_id.is_(None),
                    FileObject.folder_id.is_(None),
                )
            )
            # Remove orphan settings after a retention period too.
            await s.execute(
                delete(User).where(
                    User.created_at < now() - timedelta(days=self.config.history_ttl_days),
                    ~select(Job.id).where(Job.owner_id == User.id).exists(),
                    ~select(Workspace.id).where(Workspace.owner_id == User.id).exists(),
                )
            )

    async def save_result(self, job_id, token, result, metrics):
        async with self.sessions.begin() as s:
            j = await s.get(Job, job_id, with_for_update=True)
            if not self.writable(j, token):
                return False
            u = await s.get(User, j.owner_id)
            if not u or u.generation != j.generation:
                return False
            if len(json.dumps(result, ensure_ascii=False).encode()) > self.config.tools_output_bytes:
                raise Limited()
            j.result, j.metrics = result, metrics
            return True

    async def task_progress(self, job_id, token, phase, **counts):
        async with self.sessions.begin() as s:
            j = await s.get(Job, job_id, with_for_update=True)
            if self.writable(j, token):
                j.progress = {"module": {"phase": phase, **counts}}

    async def enrich_state(self, job_id, generation, state, token):
        async with self.sessions.begin() as s:
            j = await s.get(Job, job_id, with_for_update=True)
            if j and not j.cancelled and j.generation == generation and j.lease == token:
                j.enrichment = state
                j.lease, j.lease_until = None, None

    async def enrichment_heartbeat(self, job_id, token):
        async with self.sessions.begin() as s:
            j = await s.get(Job, job_id, with_for_update=True)
            if not j or j.cancelled or j.lease != token or j.enrichment != "RUNNING":
                return False
            j.lease_until = self.lease_deadline()
            return True

    async def enrich_claim(self):
        async with self.sessions.begin() as s:
            j = await s.scalar(
                select(Job)
                .where(
                    Job.state.in_([JobState.COMPLETED, JobState.PARTIAL]),
                    Job.cancelled.is_(False),
                    Job.expires_at > now(),
                    or_(
                        Job.enrichment == "PENDING", (Job.enrichment == "RUNNING") & (Job.lease_until < now())
                    ),
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if j:
                j.enrichment, j.lease_until = "RUNNING", self.lease_deadline()
                j.lease = str(uuid.uuid4())
                return j


def as_utc(dt):
    from datetime import UTC

    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def site_dict(s):
    return {
        k: getattr(s, k)
        for k in (
            "name",
            "category",
            "description",
            "activities",
            "sources",
            "origin",
            "reviewed",
            "schema_version",
            "prompt_version",
        )
    }
