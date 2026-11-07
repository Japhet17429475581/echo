import asyncio
import json
import time

from echo.comparison import compare
from echo.export import Reports, encrypt
from echo.files import Vault
from echo.modules import REGISTRY, ModuleError, Modules, validate_input
from echo.packages import isolated_package, isolated_report, json_bytes, publish_package
from echo.services import Denied
from echo.storage import FileObject
from echo.workspaces import Folders


class Tasks:
    def __init__(self, config, jobs, redis, limiter):
        self.config, self.jobs, self.redis = config, jobs, redis
        self.vault = Vault(jobs.sessions, config)
        self.reports = Reports(jobs, config)
        self.folders = Folders(jobs, self.vault, self.reports, config)
        self.modules = Modules(config, jobs, self.vault, limiter)

    async def availability(self):
        available = {key: False for key in REGISTRY}
        async for key in self.redis.scan_iter(match="echo:tools:*"):
            raw = await self.redis.get(key)
            if raw:
                for module, ready in json.loads(raw).get("available", {}).items():
                    if module in REGISTRY and REGISTRY[module].enabled(self.config) and ready:
                        available[module] = True
        return available

    async def submit(self, owner, module, data, request_key):
        parameters = validate_input(module, data, self.config)
        await self.vault.space()
        if not (await self.availability()).get(module):
            raise ModuleError("module_unavailable")
        if "file_id" in parameters:
            await self.vault.get(owner, parameters["file_id"])
        job = await self.jobs.create(
            owner, "", [], "tool", False, request_key, kind="module", module_id=module, parameters=parameters
        )
        if "file_id" in parameters:
            async with self.jobs.sessions.begin() as s:
                file = await s.get(FileObject, parameters["file_id"], with_for_update=True)
                if not file or file.owner_id != owner:
                    raise Denied()
                if not file.job_id:
                    file.job_id, file.expires_at = job.id, job.expires_at
        return job

    async def execute(self, job):
        # Saved results fence redelivery: a crash after publication never reruns the tool.
        if job.result:
            if job.kind == "module" and not any(
                f.name == "report.json" for f in await self.vault.for_job(job.owner_id, job.id, "output")
            ):
                report = await self.vault.put(
                    job.owner_id,
                    job.generation,
                    json_bytes(job.result),
                    "report.json",
                    job_id=job.id,
                    token=job.lease,
                    role="output",
                    mime="application/json",
                )
                result = {
                    **job.result,
                    "artifacts": [
                        *job.result.get("artifacts", []),
                        {"id": report.id, "name": report.name, "size": report.size, "sha256": report.sha256},
                    ],
                }
                await self.jobs.save_result(job.id, job.lease, result, job.metrics)
            return
        for output in await self.vault.for_job(job.owner_id, job.id, "output"):
            await self.vault.discard(output)
        start = time.monotonic()
        if job.kind == "module":
            async with asyncio.timeout(getattr(self.config, job.module_id + "_timeout")):
                result = await self.modules.run(job)
            report = await self.vault.put(
                job.owner_id,
                job.generation,
                json_bytes(result),
                "report.json",
                job_id=job.id,
                token=job.lease,
                role="output",
                mime="application/json",
            )
            # `report.json` describes module facts; the ZIP manifest supplies its own hash.
            result["artifacts"].append(
                {
                    "id": report.id,
                    "name": report.name,
                    "size": report.size,
                    "sha256": report.sha256,
                    "mime": report.mime,
                }
            )
            current = await self.jobs.owned(job.owner_id, job.id)
            await self.jobs.save_result(job.id, job.lease, result, current.metrics)
        elif job.kind == "folder_export":
            async with asyncio.timeout(self.config.export_timeout):
                await self.folders.export(job)
        elif job.kind == "compare":
            if not job.parameters.get("authorized"):
                raise Denied()
            result = await compare(self.jobs, job.owner_id, job.parameters["first"], job.parameters["second"])
            report = await self.vault.put(
                job.owner_id,
                job.generation,
                json_bytes(result),
                "comparison.json",
                job_id=job.id,
                token=job.lease,
                role="output",
                mime="application/json",
            )
            result["artifacts"] = [
                {"id": report.id, "name": report.name, "size": report.size, "sha256": report.sha256}
            ]
            await self.jobs.save_result(
                job.id, job.lease, result, {"duration_seconds": time.monotonic() - start}
            )
        elif job.kind == "export":
            async with asyncio.timeout(self.config.export_timeout):
                source = await self.jobs.owned(job.owner_id, job.parameters["source_job"])
                fmt = job.parameters["format"]
                recipient = job.parameters.get("recipient")
                if fmt == "zip":
                    if source.kind == "search":
                        snapshot = await self.reports.snapshot(
                            job.owner_id, source.id, job.parameters["language"]
                        )
                        materials = [
                            ("report.json", json_bytes(snapshot), {"schema_version": "echo.report/1"})
                        ]
                    else:
                        materials = [
                            (
                                "report.json",
                                json_bytes(source.result),
                                {"schema_version": source.result["schema_version"]},
                            )
                        ]
                        for f in await self.vault.for_job(job.owner_id, source.id, "output"):
                            if f.name == "report.json":
                                continue
                            binary, _ = await self.vault.read(job.owner_id, f.id)
                            materials.append((f.name, binary, {"mime": f.mime}))
                    content = await isolated_package(
                        job,
                        materials,
                        {
                            "source_job": source.id,
                            "parameters": source.parameters,
                            "schema_versions": ["echo.package/1"],
                            "versions": source.result.get("versions", {})
                            if source.result
                            else source.progress,
                        },
                        self.config,
                        self.modules.runner,
                    )
                    artifacts = await publish_package(
                        job, content, "echo-report.zip", self.vault, self.config, recipient=recipient
                    )
                elif source.kind != "search":
                    matching = [
                        f
                        for f in await self.vault.for_job(job.owner_id, source.id, "output")
                        if f.name.endswith("." + fmt)
                    ]
                    if not matching:
                        raise ModuleError("invalid_input")
                    content, f = await self.vault.read(job.owner_id, matching[0].id)
                    name = f.name
                    if recipient:
                        content = await encrypt(content, recipient, self.config.age_binary)
                        name += ".age"
                    saved = await self.vault.put(
                        job.owner_id,
                        job.generation,
                        content,
                        name,
                        job_id=job.id,
                        token=job.lease,
                        role="output",
                        mime="application/octet-stream",
                    )
                    artifacts = [
                        {"id": saved.id, "name": saved.name, "size": saved.size, "sha256": saved.sha256}
                    ]
                else:
                    if fmt == "pdf":
                        snapshot = await self.reports.snapshot(
                            job.owner_id, source.id, job.parameters["language"]
                        )
                        content = await isolated_report(
                            job, snapshot, fmt, job.parameters["language"], self.config, self.modules.runner
                        )
                        name = f"echo-{source.id[:8]}.pdf"
                        if recipient:
                            content = await encrypt(content, recipient, self.config.age_binary)
                            name += ".age"
                    else:
                        content, name = await self.reports.build(
                            job.owner_id, source.id, fmt, job.parameters["language"], recipient
                        )
                    f = await self.vault.put(
                        job.owner_id,
                        job.generation,
                        content,
                        name,
                        job_id=job.id,
                        token=job.lease,
                        role="output",
                        mime="application/octet-stream",
                    )
                    artifacts = [{"id": f.id, "name": f.name, "size": f.size, "sha256": f.sha256}]
                await self.jobs.owned(job.owner_id, source.id)
                await self.jobs.save_result(
                    job.id,
                    job.lease,
                    {"schema_version": "echo.export/1", "artifacts": artifacts},
                    {"duration_seconds": time.monotonic() - start},
                )
