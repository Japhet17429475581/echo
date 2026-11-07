import re
from datetime import timedelta

from sqlalchemy import delete, func, select

from echo.domain import now
from echo.modules import LocalRunner
from echo.packages import isolated_package, json_bytes, publish_package
from echo.services import Denied, FolderLimited, Limited, as_utc
from echo.storage import FileObject, Job, User, Workspace, WorkspaceItem


class Folders:
    def __init__(self, jobs, vault, reports, config):
        self.jobs, self.vault, self.reports, self.config = jobs, vault, reports, config
        self.sessions = jobs.sessions
        self.runner = LocalRunner(config)

    async def list(self, owner):
        async with self.sessions() as s:
            return list(
                await s.scalars(
                    select(Workspace)
                    .where(Workspace.owner_id == owner, Workspace.expires_at > now())
                    .order_by(Workspace.created_at)
                )
            )

    async def owned(self, owner, folder_id):
        async with self.sessions() as s:
            f = await s.get(Workspace, folder_id)
            if not f or f.owner_id != owner or as_utc(f.expires_at) <= now():
                raise Denied()
            return f

    async def create(self, owner, name):
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("folder_name")
        async with self.sessions.begin() as s:
            if not await s.get(User, owner, with_for_update=True):
                raise Denied()
            count = await s.scalar(
                select(func.count()).select_from(Workspace).where(Workspace.owner_id == owner)
            )
            if count >= self.config.max_folders:
                raise Limited()
            f = Workspace(
                owner_id=owner, name=name, expires_at=now() + timedelta(days=self.config.folder_ttl_days)
            )
            s.add(f)
            await s.flush()
            return f

    async def rename(self, owner, folder_id, name):
        if not 1 <= len(name.strip()) <= 80:
            raise ValueError("folder_name")
        async with self.sessions.begin() as s:
            f = await s.get(Workspace, folder_id, with_for_update=True)
            if not f or f.owner_id != owner:
                raise Denied()
            f.name = name.strip()

    async def items(self, owner, folder_id):
        await self.owned(owner, folder_id)
        async with self.sessions() as s:
            return list(
                await s.scalars(
                    select(WorkspaceItem)
                    .where(WorkspaceItem.folder_id == folder_id)
                    .order_by(WorkspaceItem.created_at)
                )
            )

    async def add(self, owner, folder_id, kind, title, data):
        if len(json_bytes(data)) > self.config.tools_output_bytes:
            raise Limited()
        async with self.sessions.begin() as s:
            f = await s.get(Workspace, folder_id, with_for_update=True)
            if not f or f.owner_id != owner or as_utc(f.expires_at) <= now():
                raise Denied()
            count = await s.scalar(
                select(func.count()).select_from(WorkspaceItem).where(WorkspaceItem.folder_id == folder_id)
            )
            if count >= self.config.max_folder_items:
                raise Limited()
            i = WorkspaceItem(folder_id=folder_id, kind=kind, title=title[:120], data=data)
            s.add(i)
            await s.flush()
            return i

    async def add_file(self, owner, folder_id, file_id):
        u = await self.jobs.user(owner)
        data, source = await self.vault.read(owner, file_id)
        copy = await self.vault.put(
            owner, u.generation, data, source.name, folder_id=folder_id, role="material", mime=source.mime
        )
        try:
            return await self.add(
                owner, folder_id, "file", source.name, {"file_ids": [copy.id], "sha256": copy.sha256}
            )
        except BaseException:
            await self.vault.discard(copy)
            raise

    async def add_job(self, owner, folder_id, job_id, language, card_prefix=None):
        j = await self.jobs.owned(owner, job_id)
        if j.kind == "search":
            data = await self.reports.snapshot(owner, job_id, language)
            if card_prefix:
                data["results"] = [c for c in data["results"] if c["key"].startswith(card_prefix)]
                if len(data["results"]) != 1:
                    raise Denied()
        else:
            if not j.result:
                raise Limited()
            data = dict(j.result)
        copies = []
        try:
            if j.kind != "search":
                u = await self.jobs.user(owner)
                for source in await self.vault.for_job(owner, job_id, "output"):
                    content, _ = await self.vault.read(owner, source.id)
                    copy = await self.vault.put(
                        owner,
                        u.generation,
                        content,
                        source.name,
                        folder_id=folder_id,
                        role="material",
                        mime=source.mime,
                    )
                    copies.append(copy)
            data = {"snapshot": data, "file_ids": [f.id for f in copies], "saved_at": now().isoformat()}
            return await self.add(
                owner,
                folder_id,
                "search_result" if j.kind == "search" else "module_report",
                j.username or j.module_id or j.kind,
                data,
            )
        except BaseException:
            for copy in copies:
                await self.vault.discard(copy)
            raise

    async def remove_item(self, owner, folder_id, item_id):
        await self.owned(owner, folder_id)
        async with self.sessions.begin() as s:
            i = await s.get(WorkspaceItem, item_id, with_for_update=True)
            if not i or i.folder_id != folder_id:
                raise Denied()
            files = list(
                await s.scalars(
                    select(FileObject).where(
                        FileObject.folder_id == folder_id, FileObject.id.in_(i.data.get("file_ids", []))
                    )
                )
            )
            await s.delete(i)
            for file in files:
                await s.delete(file)
        await self.vault.janitor()

    async def add_cards(self, owner, folder_id, job_id, keys, language, request_key):
        """Save a bounded selection atomically; repeated confirmation never duplicates it."""
        keys = list(dict.fromkeys(keys))
        if not keys or len(keys) > self.config.batch_max_cards:
            raise FolderLimited()
        if any(not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key) for key in keys):
            raise Denied()
        source = await self.jobs.owned(owner, job_id)
        if source.kind != "search" or not request_key or len(request_key) > 100:
            raise Denied()
        snapshot = await self.reports.snapshot(owner, job_id, language)
        cards = {c["key"]: c for c in snapshot["results"]}
        if set(keys) - cards.keys():
            raise Denied()
        payloads = [
            {
                "snapshot": {
                    **snapshot,
                    "results": [cards[key]],
                    "selection": {"keys": [key], "source_result_count": len(cards)},
                },
                "saved_at": now().isoformat(),
                "selection_request": request_key,
            }
            for key in keys
        ]
        if any(len(json_bytes(data)) > self.config.tools_output_bytes for data in payloads):
            raise FolderLimited()
        async with self.sessions.begin() as s:
            user = await s.get(User, owner, with_for_update=True)
            folder = await s.get(Workspace, folder_id, with_for_update=True)
            job = await s.get(Job, job_id, with_for_update=True)
            if (
                not user
                or not folder
                or folder.owner_id != owner
                or as_utc(folder.expires_at) <= now()
                or not job
                or job.owner_id != owner
                or job.generation != user.generation
                or as_utc(job.expires_at) <= now()
            ):
                raise Denied()
            existing = list(
                await s.scalars(
                    select(WorkspaceItem).where(
                        WorkspaceItem.folder_id == folder_id,
                        WorkspaceItem.data["selection_request"].as_string() == request_key,
                    )
                )
            )
            if existing:
                saved_keys = {i.data["snapshot"]["results"][0]["key"] for i in existing}
                if saved_keys != set(keys) or any(i.data["snapshot"]["job_id"] != job_id for i in existing):
                    raise Denied()
                return existing
            count = await s.scalar(
                select(func.count()).select_from(WorkspaceItem).where(WorkspaceItem.folder_id == folder_id)
            )
            if count + len(keys) > self.config.max_folder_items:
                raise FolderLimited()
            items = [
                WorkspaceItem(
                    folder_id=folder_id, kind="search_result", title=cards[key]["name"][:120], data=data
                )
                for key, data in zip(keys, payloads, strict=True)
            ]
            s.add_all(items)
            await s.flush()
            return items

    async def delete(self, owner, folder_id):
        async with self.sessions.begin() as s:
            await s.get(User, owner, with_for_update=True)
            f = await s.get(Workspace, folder_id, with_for_update=True)
            if not f or f.owner_id != owner:
                raise Denied()
            await s.execute(
                delete(Job).where(
                    Job.owner_id == owner,
                    Job.kind == "folder_export",
                    Job.parameters["folder_id"].as_string() == folder_id,
                )
            )
            await s.delete(f)
        await self.vault.janitor()

    async def export(self, job):
        folder_id = job.parameters["folder_id"]
        f = await self.owned(job.owner_id, folder_id)
        items = await self.items(job.owner_id, folder_id)
        materials, total = [], 0
        for i in items:
            content = json_bytes(
                {"schema_version": "echo.folder-item/1", "kind": i.kind, "title": i.title, "data": i.data}
            )
            materials.append((i.kind + ".json", content, {"schema_version": "echo.folder-item/1"}))
            total += len(content)
            for file_id in i.data.get("file_ids", []):
                entry = await self.vault.get(job.owner_id, file_id)
                total += entry.size
                if total > self.config.export_max_mb * 1024**2:
                    raise Limited()
                binary, entry = await self.vault.read(job.owner_id, file_id)
                materials.append((entry.name, binary, {"mime": entry.mime}))
        archive = await isolated_package(
            job,
            materials,
            {
                "type": "workspace",
                "name": f.name,
                "schema_versions": ["echo.folder-item/1"],
                "tool_versions_in_material_snapshots": True,
            },
            self.config,
            self.runner,
        )
        await self.owned(job.owner_id, folder_id)
        files = await publish_package(
            job,
            archive,
            "echo-folder.zip",
            self.vault,
            self.config,
            recipient=job.parameters.get("recipient"),
        )
        result = {
            "schema_version": "echo.folder-export/1",
            "folder_name": f.name,
            "count": len(items),
            "artifacts": files,
        }
        await self.jobs.save_result(
            job.id, job.lease, result, {"result_bytes": sum(f["size"] for f in files)}
        )
        return result
