import asyncio
import hashlib
import os
import re
import shutil
import uuid
from datetime import timedelta

from sqlalchemy import delete, func, select

from echo.domain import now
from echo.services import Denied, Jobs, StorageLimited, as_utc
from echo.storage import FileObject, Gate, Job, User, Workspace


def display_name(value):
    value = str(value or "document").replace("\\", "/").split("/")[-1]
    value = "".join(c for c in value if ord(c) >= 32 and ord(c) != 127).strip(" .")
    return value[:120] or "document"


class Vault:
    """Owned files, opaque paths, DB reservations and a bounded shared volume."""

    def __init__(self, sessions, config):
        self.sessions, self.config = sessions, config

    def path(self, key):
        if not re.fullmatch(r"[a-f0-9]{32}", key):
            raise Denied()
        return self.config.data_dir / key

    async def space(self):
        await asyncio.to_thread(self.config.data_dir.mkdir, parents=True, exist_ok=True, mode=0o700)
        free = (await asyncio.to_thread(shutil.disk_usage, self.config.data_dir)).free
        if free < self.config.disk_min_free_mb * 1024 * 1024:
            raise StorageLimited()
        return free

    async def reserve(
        self,
        owner,
        generation,
        size,
        name,
        *,
        job_id=None,
        folder_id=None,
        role="input",
        token=None,
        mime="application/octet-stream",
    ):
        if size < 0 or size > max(self.config.file_max_mb, self.config.export_max_mb) * 1024 * 1024:
            raise StorageLimited()
        await self.space()
        async with self.sessions.begin() as s:
            await s.get(Gate, 1, with_for_update=True)
            u = await s.get(User, owner, with_for_update=True)
            if not u or u.generation != generation:
                raise Denied()
            deadline = now() + timedelta(hours=self.config.result_ttl_hours)
            if job_id:
                j = await s.get(Job, job_id, with_for_update=True)
                if (
                    not j
                    or j.owner_id != owner
                    or j.generation != generation
                    or j.cancelled
                    or as_utc(j.expires_at) <= now()
                    or token
                    and not Jobs.writable(j, token)
                ):
                    raise Denied()
                deadline = j.expires_at
            if folder_id:
                f = await s.get(Workspace, folder_id, with_for_update=True)
                if not f or f.owner_id != owner or as_utc(f.expires_at) <= now():
                    raise Denied()
                deadline = f.expires_at
            total = await s.scalar(select(func.coalesce(func.sum(FileObject.size), 0)))
            user_size = await s.scalar(
                select(func.coalesce(func.sum(FileObject.size), 0)).where(FileObject.owner_id == owner)
            )
            if (
                total + size > self.config.disk_quota_mb * 1024**2
                or user_size + size > self.config.user_disk_quota_mb * 1024**2
            ):
                raise StorageLimited()
            entry = FileObject(
                owner_id=owner,
                generation=generation,
                job_id=job_id,
                folder_id=folder_id,
                size=size,
                name=display_name(name),
                role=role,
                mime=mime,
                storage_key=uuid.uuid4().hex,
                expires_at=deadline,
            )
            s.add(entry)
            await s.flush()
            return entry

    async def finalize(self, entry, *, token=None):
        path = self.path(entry.storage_key)
        size = (await asyncio.to_thread(path.stat)).st_size
        if size > entry.size:
            await self.discard(entry)
            raise StorageLimited()
        digest = await asyncio.to_thread(self.digest, path)
        async with self.sessions.begin() as s:
            # Same lock order as deletion. Old generations never republish files.
            u = await s.get(User, entry.owner_id, with_for_update=True)
            current = await s.get(FileObject, entry.id, with_for_update=True)
            if not u or u.generation != entry.generation or not current:
                await asyncio.to_thread(path.unlink, missing_ok=True)
                raise Denied()
            if current.job_id:
                job = await s.get(Job, current.job_id)
                if not job or job.cancelled or token and not Jobs.writable(job, token):
                    raise Denied()
            current.size, current.sha256, current.state = size, digest, "READY"
            await s.flush()
            return current

    @staticmethod
    def digest(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    async def put(self, owner, generation, data, name, **kwargs):
        entry = await self.reserve(owner, generation, len(data), name, **kwargs)
        try:
            await asyncio.to_thread(self._write, self.path(entry.storage_key), data)
            return await self.finalize(entry, token=kwargs.get("token"))
        except BaseException:
            await self.discard(entry)
            raise

    @staticmethod
    def _write(path, data):
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)

    async def upload(self, owner, generation, source, size, name):
        """Streaming Telegram download; never buffers an unbounded file."""
        if size <= 0 or size > self.config.file_max_mb * 1024**2:
            raise StorageLimited()
        entry = await self.reserve(owner, generation, size, name)
        try:
            destination = self.path(entry.storage_key)
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            count = 0
            with os.fdopen(fd, "wb") as stream:
                async for chunk in source:
                    count += len(chunk)
                    if count > size or count > self.config.file_max_mb * 1024**2:
                        raise StorageLimited()
                    await asyncio.to_thread(stream.write, chunk)
                    await self.space()
            if count == 0:
                raise ValueError("empty_file")
            return await self.finalize(entry)
        except BaseException:
            await self.discard(entry)
            raise

    async def get(self, owner, file_id):
        async with self.sessions() as s:
            entry = await s.get(FileObject, file_id)
            if (
                not entry
                or entry.owner_id != owner
                or entry.state != "READY"
                or as_utc(entry.expires_at) <= now()
            ):
                raise Denied()
            if entry.job_id:
                j = await s.get(Job, entry.job_id)
                if not j or j.owner_id != owner or as_utc(j.expires_at) <= now():
                    raise Denied()
            if entry.folder_id:
                f = await s.get(Workspace, entry.folder_id)
                if not f or f.owner_id != owner or as_utc(f.expires_at) <= now():
                    raise Denied()
        return entry

    async def read(self, owner, file_id):
        entry = await self.get(owner, file_id)
        data = await asyncio.to_thread(self.path(entry.storage_key).read_bytes)
        if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise ValueError("checksum_mismatch")
        await self.get(owner, file_id)
        return data, entry

    async def for_job(self, owner, job_id, role=None):
        async with self.sessions() as s:
            query = select(FileObject).where(
                FileObject.owner_id == owner,
                FileObject.job_id == job_id,
                FileObject.state == "READY",
                FileObject.expires_at > now(),
            )
            if role:
                query = query.where(FileObject.role == role)
            return list(await s.scalars(query))

    async def discard(self, entry):
        async with self.sessions.begin() as s:
            await s.execute(delete(FileObject).where(FileObject.id == entry.id))
        await asyncio.to_thread(self.path(entry.storage_key).unlink, missing_ok=True)

    async def erase_owner(self, owner):
        # Called while Jobs holds the user lock; workers cannot publish new artifacts.
        async with self.sessions() as s:
            entries = list(await s.scalars(select(FileObject).where(FileObject.owner_id == owner)))
        for entry in entries:
            await asyncio.to_thread(self.path(entry.storage_key).unlink, missing_ok=True)

    async def janitor(self):
        # Cleanup must still work when free space is below the admission floor.
        await asyncio.to_thread(self.config.data_dir.mkdir, parents=True, exist_ok=True, mode=0o700)
        async with self.sessions() as s:
            live = set(await s.scalars(select(FileObject.storage_key)))
        for path in await asyncio.to_thread(lambda: list(self.config.data_dir.iterdir())):
            if path.name not in live and re.fullmatch(r"[a-f0-9]{32}", path.name) and not path.is_symlink():
                # DB reservation precedes creation: an active file is always in `live`.
                async with self.sessions() as s:
                    exists = await s.scalar(select(FileObject.id).where(FileObject.storage_key == path.name))
                if not exists:
                    await asyncio.to_thread(path.unlink, missing_ok=True)
