import asyncio
import contextlib
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

import psutil

from echo.domain import Engine, Observation, Status, normalize

RUNNER = Path(__file__).resolve().parents[2] / "engines/runner.py"


class EngineError(Exception):
    def __init__(self, code):
        self.code = code


class DomainLimiter:
    def __init__(self, redis, interval):
        self.redis, self.interval = redis, interval

    async def wait(self, domains):
        # A short expiring slot per exact host (including private PSL tenants).
        for domain in sorted(set(domains)):
            import hashlib

            key = "echo:domain:" + hashlib.sha256(domain.lower().encode()).hexdigest()
            while not await self.redis.set(key, "1", nx=True, px=int(self.interval * 1000)):  # noqa: ASYNC110
                await asyncio.sleep(min(self.interval, 0.25))


async def kill_tree(process):
    if process.returncode is not None:
        return
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 2)
            return
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    else:
        with contextlib.suppress(psutil.NoSuchProcess):
            root = psutil.Process(process.pid)
            for child in root.children(recursive=True):
                with contextlib.suppress(psutil.NoSuchProcess):
                    child.kill()
            root.kill()
    await process.wait()


class Adapter:
    def __init__(self, engine: Engine, config, limiter: DomainLimiter):
        self.engine, self.config, self.limiter = engine, config, limiter

    async def run(self, job, skip, on_result, on_progress):
        python = self.config.sherlock_python if self.engine == Engine.SHERLOCK else self.config.maigret_python
        request = {
            "username": job.username,
            "coverage": job.coverage,
            "targets": job.targets.get(self.engine, []) if job.targets else None,
            "skip": skip,
            "max_sites": self.config.max_sites,
            "timeout": self.config.site_timeout,
            "memory_mb": self.config.engine_memory_mb,
        }
        self.config.temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="engine-", dir=self.config.temp_dir) as work:
            if getattr(job, "id", None) and getattr(job, "lease", None):
                await asyncio.to_thread(
                    Path(work, "lease.json").write_text,
                    json.dumps({"job": job.id, "lease": job.lease}),
                    "utf-8",
                )
            env = {
                k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "WINDIR", "SSL_CERT_FILE"}
            }
            env.update(
                {
                    "PYTHONUTF8": "1",
                    "PYTHONUNBUFFERED": "1",
                    "HOME": work,
                    "TMPDIR": work,
                    "TEMP": work,
                    "TMP": work,
                    "OPENBLAS_NUM_THREADS": "1",
                    "OMP_NUM_THREADS": "1",
                }
            )
            process = await asyncio.create_subprocess_exec(
                python,
                str(RUNNER),
                self.engine.value,
                cwd=work,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=sys.platform != "win32",
                limit=64 * 1024,
            )
            try:
                process.stdin.write((json.dumps(request) + "\n").encode())
                await process.stdin.drain()
                async with asyncio.timeout(self.config.engine_timeout):
                    done, completed, matches, errors, total_bytes = False, len(skip), 0, 0, 0
                    info = {"completed": completed, "total": None}
                    async for raw in process.stdout:
                        total_bytes += len(raw)
                        if total_bytes > self.config.engine_output_mb * 1024 * 1024:
                            raise EngineError("output_limit")
                        try:
                            event = json.loads(raw)
                        except (ValueError, UnicodeError) as e:
                            raise EngineError("invalid_protocol") from e
                        kind = event.get("type")
                        if kind == "ready":
                            info.update(total=event["total"] + len(skip), version=event["version"])
                            await on_progress(info)
                        elif kind == "permit":
                            if await self.limiter.redis.exists(f"echo:disabled:{self.engine}"):
                                raise EngineError("disabled")
                            await self.limiter.wait(event["domains"])
                            process.stdin.write(b"continue\n")
                            await process.stdin.drain()
                        elif kind == "result":
                            o = Observation.model_validate(event["data"])
                            o.status = normalize(o)
                            if o.raw_status.lower() == "waf" or o.error_type == "blocked":
                                o.status = Status.BLOCKED
                            if o.engine != self.engine or o.username != job.username:
                                raise EngineError("invalid_protocol")
                            await on_result(o)
                            completed += 1
                            matches += o.status == Status.POSSIBLE_MATCH
                            errors += o.status not in {Status.POSSIBLE_MATCH, Status.NOT_FOUND}
                            info.update(completed=completed, matches=matches, errors=errors)
                            await on_progress(info)
                        elif kind == "done":
                            done = True
                        elif kind == "warning":
                            info["warning"] = event["code"]
                            await on_progress(info)
                        elif kind == "fatal":
                            raise EngineError("engine_failure")
                        else:
                            raise EngineError("invalid_protocol")
                    await process.wait()
                    if process.returncode or not done:
                        raise EngineError("engine_failure")
                    if info.get("warning"):
                        raise EngineError(info["warning"])
            except TimeoutError as e:
                raise EngineError("timeout") from e
            finally:
                await kill_tree(process)
