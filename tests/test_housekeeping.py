import asyncio
import errno
import json
import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from echo import bootstrap
from echo.diagnostics import failure
from echo.worker import Worker


def orphan(config):
    work = config.temp_dir / "module-crashed"
    work.mkdir(parents=True)
    (work / "lease.json").write_text(json.dumps({"job": "missing-job", "lease": "old"}))
    (work / "partial.bin").write_bytes(b"unfinished fixture")
    return work


async def test_file_permission_failure_is_diagnosed_without_blocking_scratch(
    config, jobs, redis, monkeypatch, caplog
):
    worker = Worker(config, jobs, redis, None)
    work = orphan(config)
    private_message = "do-not-log-fixture-secret"
    janitor = AsyncMock(side_effect=PermissionError(errno.EACCES, private_message))
    monkeypatch.setattr(worker.tasks.vault, "janitor", janitor)
    with caplog.at_level(logging.WARNING, logger="echo"):
        assert not await worker.cleanup_once()
    assert "cleanup_failed stage=files error=PermissionError errno=13" in caplog.text
    assert private_message not in caplog.text and not work.exists()
    assert await redis.get("echo:worker:" + worker.id) == "alive"
    janitor.side_effect = None
    assert await worker.cleanup_once()


async def test_cache_outage_does_not_block_file_cleanup(config, jobs, redis, monkeypatch, caplog):
    worker = Worker(config, jobs, redis, None)
    work = orphan(config)
    config.data_dir.mkdir()
    abandoned = config.data_dir / ("a" * 32)
    abandoned.write_bytes(b"orphan fixture")
    monkeypatch.setattr(redis, "set", AsyncMock(side_effect=TimeoutError("do-not-log-connection")))
    with caplog.at_level(logging.WARNING, logger="echo"):
        assert not await worker.cleanup_once()
    assert not work.exists() and not abandoned.exists()
    assert "stage=heartbeat error=TimeoutError" in caplog.text
    assert "do-not-log-connection" not in caplog.text


async def test_maintenance_propagates_cancellation(config, jobs, redis, monkeypatch):
    worker = Worker(config, jobs, redis, None)
    monkeypatch.setattr(redis, "set", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await worker.cleanup_once()


def test_diagnostic_logs_sqlstate_without_query_or_parameters(caplog):
    error = RuntimeError("do-not-log-sql-and-parameters")
    error.orig = SimpleNamespace(sqlstate="42P01")
    with caplog.at_level(logging.WARNING, logger="echo"):
        failure(logging.getLogger("echo"), "cleanup_failed", "jobs", error)
    assert "sqlstate=42P01" in caplog.text
    assert "do-not-log-sql-and-parameters" not in caplog.text


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bootstrap filesystem contract")
def test_private_directory_preparation_preserves_parent_and_contents(tmp_path):
    parent = tmp_path / "mount"
    parent.mkdir(mode=0o755)
    original = parent.stat()
    private = parent / "echo"
    bootstrap.prepare_directory(private)
    record = private / "existing.bin"
    record.write_bytes(b"preserved fixture")
    bootstrap.prepare_directory(private)
    assert record.read_bytes() == b"preserved fixture"
    assert private.stat().st_mode & 0o777 == 0o700
    assert (parent.stat().st_uid, parent.stat().st_mode) == (original.st_uid, original.st_mode)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux O_NOFOLLOW contract")
def test_private_directory_preparation_rejects_symlink(tmp_path):
    outside = tmp_path / "other-directory"
    outside.mkdir(mode=0o755)
    original = outside.stat()
    link = tmp_path / "configured-directory"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        bootstrap.prepare_directory(link)
    assert (outside.stat().st_uid, outside.stat().st_mode) == (original.st_uid, original.st_mode)


def test_bootstrap_drops_privileges_before_exec(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(sys, "argv", ["bootstrap", "python", "-m", "echo.main", "serve"])
    monkeypatch.setattr(bootstrap, "settings", lambda: SimpleNamespace(data_dir=tmp_path, temp_dir=tmp_path))
    monkeypatch.setattr(bootstrap, "prepare_directory", lambda path: events.append("prepare"))
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(os, "setgroups", lambda groups: events.append(("groups", groups)), raising=False)
    monkeypatch.setattr(os, "setgid", lambda value: events.append(("gid", value)), raising=False)
    monkeypatch.setattr(os, "setuid", lambda value: events.append(("uid", value)), raising=False)
    monkeypatch.setenv("HOME", "fixture")

    class Executed(BaseException):
        pass

    def execute(program, command, environment):
        assert events[-3:] == [("groups", []), ("gid", 10001), ("uid", 10001)]
        assert program == "python" and command[-1] == "serve"
        assert environment["HOME"] == "/home/echo"
        raise Executed()

    monkeypatch.setattr(os, "execvpe", execute)
    with pytest.raises(Executed):
        bootstrap.main()
