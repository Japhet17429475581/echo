import asyncio
import hashlib
import io
import json
import tempfile
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

from echo.domain import now
from echo.export import ExportError, encrypt
from echo.files import display_name


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode()


def package(materials, metadata, config):
    maximum = config.export_max_mb * 1024**2
    if sum(len(content) for _, content, _ in materials) > maximum:
        raise ExportError("file_too_large")
    manifest = {
        "schema_version": "echo.package/1",
        "created_at": now().astimezone(ZoneInfo(config.timezone)).isoformat(),
        "timezone": config.timezone,
        "metadata": metadata,
        "files": [],
        "limitations": "Checksums detect changes; they do not prove source authenticity.",
        "self_checksum": "manifest is excluded from its own hashes; use the archive .sha256 sidecar",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=5) as archive:
        for index, (name, content, detail) in enumerate(materials):
            # Never use paths supplied by an upload, source page, or archive.
            filename = f"materials/{index + 1:03d}-" + display_name(name)
            archive.writestr(filename, content)
            manifest["files"].append(
                {
                    "path": filename,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    **detail,
                }
            )
            if output.tell() > maximum:
                raise ExportError("file_too_large")
        archive.writestr("manifest.json", json_bytes(manifest))
    content = output.getvalue()
    if len(content) > maximum:
        raise ExportError("file_too_large")
    return content


async def isolated_package(job, materials, metadata, config, runner):
    if sum(len(content) for _, content, _ in materials) > config.export_max_mb * 1024**2:
        raise ExportError("file_too_large")
    config.temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(
        prefix=f"module-{job.id}-{job.lease}-", dir=config.temp_dir
    ) as directory:
        work = Path(directory)
        (work / "lease.json").write_text(json.dumps({"job": job.id, "lease": job.lease}), "utf-8")
        payload = {"materials": [], "metadata": metadata}
        for index, (name, content, detail) in enumerate(materials):
            filename = f"material-{index}"
            await asyncio.to_thread((work / filename).write_bytes, content)
            payload["materials"].append({"path": filename, "name": name, "detail": detail})
        source = work / "payload.json"
        await asyncio.to_thread(source.write_bytes, json_bytes(payload))
        await runner.execute(
            "package",
            {"timezone": config.timezone, "export_mb": config.export_max_mb},
            work=work,
            source=source,
        )
        return await asyncio.to_thread((work / "archive.zip").read_bytes)


async def isolated_report(job, snapshot, fmt, language, config, runner):
    config.temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(
        prefix=f"module-{job.id}-{job.lease}-", dir=config.temp_dir
    ) as directory:
        work = Path(directory)
        (work / "lease.json").write_text(json.dumps({"job": job.id, "lease": job.lease}), "utf-8")
        source = work / "snapshot.json"
        await asyncio.to_thread(source.write_bytes, json_bytes(snapshot))
        await runner.execute(
            "report",
            {
                "timezone": config.timezone,
                "export_mb": config.export_max_mb,
                "pdf_font": str(config.pdf_font),
                "language": language,
                "format": fmt,
            },
            work=work,
            source=source,
        )
        return await asyncio.to_thread((work / ("report." + fmt)).read_bytes)


async def publish_package(job, content, filename, vault, config, *, recipient=None):
    if recipient:
        content = await encrypt(content, recipient, config.age_binary)
        filename += ".age"
    if len(content) > min(config.export_max_mb, config.telegram_file_mb) * 1024**2:
        raise ExportError("file_too_large")
    result = await vault.put(
        job.owner_id,
        job.generation,
        content,
        filename,
        job_id=job.id,
        token=job.lease,
        role="output",
        mime="application/octet-stream",
    )
    sidecar = f"{result.sha256}  {filename}\n".encode()
    checksum = await vault.put(
        job.owner_id,
        job.generation,
        sidecar,
        filename + ".sha256",
        job_id=job.id,
        token=job.lease,
        role="output",
        mime="text/plain",
    )
    return [
        {"id": f.id, "name": f.name, "size": f.size, "sha256": f.sha256, "mime": f.mime}
        for f in (result, checksum)
    ]
