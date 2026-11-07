import asyncio
import contextlib
import importlib.metadata
import importlib.util
import ipaddress
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from echo.domain import now, safe_url
from echo.engines import kill_tree
from echo.services import as_utc
from echo.sources import SafeNetwork, public_target

TOOLS_RUNNER = Path(__file__).resolve().parents[2] / "tools/runner.py"


class ModuleError(ValueError):
    pass


@dataclass(frozen=True)
class Module:
    id: str
    input_types: tuple
    load: str
    schema: str = "echo.module/1"

    def enabled(self, config):
        return getattr(config, self.id + "_enabled")

    def limits(self, config):
        return {
            "timeout": getattr(config, self.id + "_timeout"),
            "memory_mb": getattr(config, self.id + "_memory_mb"),
            "input_bytes": config.file_max_mb * 1024**2
            if "file" in self.input_types
            else config.net_response_bytes,
            "output_bytes": config.tools_output_bytes,
        }


REGISTRY = {
    m.id: m
    for m in [
        Module("metadata", ("file",), "heavy"),
        Module("ocr", ("file",), "heavy"),
        Module("qr", ("file",), "heavy"),
        Module("passport", ("url", "domain"), "network"),
        Module("page", ("url",), "network"),
        Module("domains", ("domain",), "network"),
    ]
}


def validate_input(module, data, config):
    if module not in REGISTRY or not REGISTRY[module].enabled(config):
        raise ModuleError("module_unavailable")
    if module in {"metadata", "ocr", "qr"}:
        import uuid

        try:
            uuid.UUID(data["file_id"])
        except (KeyError, ValueError, TypeError) as e:
            raise ModuleError("invalid_file") from e
        language = data.get("language", "rus+eng")
        if language not in {"rus", "eng", "rus+eng"}:
            raise ModuleError("invalid_language")
        action = data.get("action", "read")
        if action not in {"read", "clean"} or action == "clean" and module != "metadata":
            raise ModuleError("invalid_input")
        return {"file_id": data["file_id"], "language": language, "action": action}
    url = public_target(data.get("target", ""))
    if module == "domains":
        p = urlsplit(url)
        if p.path != "/" or p.query or p.fragment or not data.get("authorized"):
            raise ModuleError("authorization_required")
        try:
            ipaddress.ip_address(p.hostname)
        except ValueError:
            pass
        else:
            raise ModuleError("invalid_domain")
        variants = int(data.get("variants", config.domain_variants))
        if not 1 <= variants <= config.domain_variants:
            raise ModuleError("variant_limit")
        return {
            "target": url,
            "domain": p.hostname,
            "authorized": True,
            "variants": variants,
            "dns": bool(data.get("dns", True)),
        }
    return {"target": url}


class LocalRunner:
    def __init__(self, config):
        self.config = config

    async def execute(self, module, parameters, *, work, source=None, progress=None):
        limit = (
            REGISTRY[module].limits(self.config)
            if module in REGISTRY
            else {"timeout": 30, "memory_mb": 384, "output_bytes": self.config.tools_output_bytes}
        )
        if module in {"report", "package"}:
            limit = {
                "timeout": self.config.export_timeout,
                "memory_mb": self.config.export_memory_mb,
                "output_bytes": self.config.tools_output_bytes,
            }
        request = {
            "module": module,
            "memory_mb": limit["memory_mb"],
            "max_pixels": self.config.image_max_pixels,
            "output_bytes": self.config.tools_output_bytes,
            "scratch_bytes": self.config.scratch_quota_mb * 1024**2,
            "cpu_seconds": self.config.tools_cpu_seconds,
            **parameters,
        }
        if source:
            await asyncio.to_thread(shutil.copyfile, source, work / "input.bin")
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "WINDIR"}}
        env.update(
            {
                "HOME": str(work),
                "TMPDIR": str(work),
                "TEMP": str(work),
                "TMP": str(work),
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUTF8": "1",
                "OMP_THREAD_LIMIT": str(self.config.ocr_threads),
                "OMP_NUM_THREADS": str(self.config.ocr_threads),
                "OPENBLAS_NUM_THREADS": "1",
            }
        )
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable if module in {"report", "package"} else self.config.tools_python,
                str(TOOLS_RUNNER),
                cwd=work,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=sys.platform != "win32",
                limit=65536,
            )
        except OSError as error:
            raise ModuleError("module_unavailable") from error
        started = time.monotonic()
        metrics = {
            "peak_tree_rss_bytes_sampled": 0,
            "peak_processes_sampled": 0,
            "peak_threads_sampled": 0,
            "cpu_tree_seconds_sampled": 0.0,
            "memory_measurement": "sum_RSS_process_tree_100ms_samples",
        }

        async def watch():
            root = psutil.Process(process.pid)
            while True:
                with contextlib.suppress(psutil.NoSuchProcess):
                    tree = [root] + root.children(recursive=True)
                    rss = cpu = threads = 0
                    for member in tree:
                        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                            rss += member.memory_info().rss
                            usage = member.cpu_times()
                            cpu += usage.user + usage.system
                            threads += member.num_threads()
                    metrics["peak_tree_rss_bytes_sampled"] = max(metrics["peak_tree_rss_bytes_sampled"], rss)
                    metrics["peak_threads_sampled"] = max(metrics["peak_threads_sampled"], threads)
                    metrics["cpu_tree_seconds_sampled"] = max(metrics["cpu_tree_seconds_sampled"], cpu)
                    metrics["peak_processes_sampled"] = max(metrics["peak_processes_sampled"], len(tree))
                    disk = await asyncio.to_thread(
                        lambda: sum(p.stat().st_size for p in work.rglob("*") if p.is_file())
                    )
                    if (
                        rss > limit["memory_mb"] * 1024**2
                        or len(tree) > self.config.tools_processes
                        or disk > self.config.scratch_quota_mb * 1024**2
                    ):
                        metrics["limit_exceeded"] = True
                        await kill_tree(process)
                        return
                await asyncio.sleep(0.1)

        watcher = asyncio.create_task(watch())
        try:
            process.stdin.write((json.dumps(request) + "\n").encode())
            await process.stdin.drain()
            process.stdin.close()
            async with asyncio.timeout(limit["timeout"]):
                output = bytearray()
                while chunk := await process.stdout.read(8192):
                    output.extend(chunk)
                    if len(output) > self.config.tools_output_bytes:
                        raise ModuleError("output_limit")
                await process.wait()
            if metrics.get("limit_exceeded"):
                raise ModuleError("resource_limit")
            try:
                response = json.loads(output)
            except ValueError as error:
                raise ModuleError("tool_failed") from error
            if process.returncode or not response.get("ok"):
                raise ModuleError(response.get("error", "tool_failed"))
            for artifact in response.get("files", []):
                p = work / artifact["path"]
                if p.is_symlink() or p.resolve().parent != work.resolve() or not p.is_file():
                    raise ModuleError("invalid_output")
            metrics["duration_seconds"] = time.monotonic() - started
            metrics["result_bytes"] = len(output)
            return response, metrics
        except TimeoutError as error:
            raise ModuleError("tool_timeout") from error
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            await kill_tree(process)

    async def probe(self):
        self.config.temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="probe-", dir=self.config.temp_dir) as scratch:
            try:
                result, _ = await self.execute("probe", {}, work=Path(scratch))
                return result["data"]
            except (ModuleError, OSError):
                return {"available": {key: False for key in REGISTRY}, "sandbox": "unavailable"}


class DNS:
    def __init__(self, config):
        self.config = config

    async def records(self, host, types=("A", "AAAA", "MX", "NS", "TXT")):
        import dns.asyncresolver
        import dns.exception
        import dns.resolver

        resolver = dns.asyncresolver.Resolver()
        resolver.lifetime = self.config.dns_timeout
        resolver.timeout = self.config.dns_timeout
        output = {}
        for kind in types:
            try:
                answer = await resolver.resolve(host, kind, search=False, lifetime=self.config.dns_timeout)
                values = [r.to_text()[:512] for r in answer][: self.config.dns_record_limit]
                output[kind] = {"status": "ANSWER", "records": values, "truncated": len(answer) > len(values)}
            except dns.resolver.NXDOMAIN:
                output[kind] = {"status": "NXDOMAIN", "records": []}
            except dns.resolver.NoAnswer:
                output[kind] = {"status": "NO_ANSWER", "records": []}
            except dns.exception.Timeout:
                output[kind] = {"status": "TIMEOUT", "records": []}
            except dns.exception.DNSException:
                output[kind] = {"status": "DNS_ERROR", "records": []}
        return output


class Modules:
    def __init__(self, config, jobs, vault, limiter, network=None, dns=None):
        self.config, self.jobs, self.vault = config, jobs, vault
        self.runner = LocalRunner(config)
        self.network, self.dns = network or SafeNetwork(config, limiter), dns or DNS(config)

    async def run(self, job):
        started = time.monotonic()
        started_at = now()
        module = job.module_id
        params = validate_input(module, job.parameters, self.config)
        await self.jobs.task_progress(job.id, job.lease, "processing")
        self.config.temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(
            prefix=f"module-{job.id}-{job.lease}-", dir=self.config.temp_dir
        ) as scratch:
            work = Path(scratch)
            (work / "lease.json").write_text(json.dumps({"job": job.id, "lease": job.lease}), "utf-8")
            source, network_data = None, {}
            if "file_id" in params:
                entry = await self.vault.get(job.owner_id, params["file_id"])
                source = self.vault.path(entry.storage_key)
                if entry.size > self.config.file_max_mb * 1024**2:
                    raise ModuleError("file_limit")
            elif module in {"page", "passport"}:
                await self.jobs.task_progress(job.id, job.lease, "fetching")
                response = await self.network.fetch(
                    params["target"],
                    truncate=module == "passport",
                    maximum=131072 if module == "passport" else None,
                )
                if module == "page" and (
                    response.status != 200
                    or not any(
                        t in response.headers.get("content-type", "")
                        for t in ("text/html", "application/xhtml+xml", "text/plain")
                    )
                ):
                    raise ModuleError("page_unavailable")
                source = work / "fetched.html"
                await asyncio.to_thread(source.write_bytes, response.body)
                params["url"] = response.final_url
                network_data = {
                    "original_url": response.original_url,
                    "final_url": response.final_url,
                    "http_status": response.status,
                    "redirects": response.redirects,
                    "headers": response.headers,
                    "body_truncated": response.truncated,
                    "fetched_at": now().isoformat(),
                }
            result, metrics = await self.runner.execute(module, params, work=work, source=source)
            data = {**result["data"], **network_data}
            if module == "passport":
                host = urlsplit(params["target"]).hostname
                data.update(domain_ascii=host, domain_unicode=host.encode().decode("idna"))
                data["dns"] = await self.dns.records(host)
                try:
                    data["tls"] = await self.network.tls(host)
                except (OSError, ValueError, TimeoutError):
                    data["tls"] = {"verified": False, "error": "tls_verification_or_connection_failed"}
                result["versions"]["dnspython"] = importlib.metadata.version("dnspython")
            if module == "domains":
                queries = 0
                for variant in data["variants"]:
                    variant["dns"] = {}
                    if params["dns"] and queries + 2 <= self.config.domain_dns_queries:
                        await self.jobs.task_progress(
                            job.id, job.lease, "dns", completed=queries // 2, total=len(data["variants"])
                        )
                        variant["dns"] = await self.dns.records(variant["domain"], ("A", "AAAA"))
                        queries += 2
                    variant["status"] = "DNS_CHECKED" if variant["dns"] else "GENERATED_ONLY"
                data["dns_queries"] = queries
            if module == "qr":
                for code in data["codes"]:
                    candidate = safe_url(code["text"])
                    code["url"] = candidate
                    code["domain"] = urlsplit(candidate).hostname if candidate else None
            artifacts = []
            for item in result["files"]:
                content = await asyncio.to_thread((work / item["path"]).read_bytes)
                saved = await self.vault.put(
                    job.owner_id,
                    job.generation,
                    content,
                    item["name"],
                    job_id=job.id,
                    token=job.lease,
                    role="output",
                    mime=item["mime"],
                )
                artifacts.append(
                    {
                        "id": saved.id,
                        "name": saved.name,
                        "size": saved.size,
                        "sha256": saved.sha256,
                        "mime": saved.mime,
                    }
                )
            metrics["input_bytes"] = (await asyncio.to_thread(source.stat)).st_size if source else 0
            metrics["queue_wait_seconds"] = max(0, (started_at - as_utc(job.created_at)).total_seconds())
            metrics["native_duration_seconds"] = metrics["duration_seconds"]
            metrics["duration_seconds"] = time.monotonic() - started
            envelope = {
                "schema_version": "echo.module/1",
                "module": module,
                "checked_at": now().isoformat(),
                "parameters": job.parameters,
                "versions": result["versions"],
                "data": data,
                "artifacts": artifacts,
                "sandbox": result["sandbox"],
            }
            if not await self.jobs.save_result(job.id, job.lease, envelope, metrics):
                raise ModuleError("cancelled")
            return envelope
