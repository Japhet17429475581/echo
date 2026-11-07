import asyncio
import socket
from unittest.mock import AsyncMock

import pytest

from echo.sources import PublicResolver, UnsafeSource, public_ip, source_url


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "192.168.0.1",
        "172.20.1.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "224.0.0.1",
    ],
)
def test_non_public_addresses(ip):
    assert not public_ip(ip)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://example.com:8000/",
        "file:///etc/passwd",
        "https://user:pass@example.com",
        "https://evil.example/",
        "http://example.com\\@127.0.0.1/",
    ],
)
def test_ssrf_url_validation(url):
    with pytest.raises(UnsafeSource):
        source_url(url, {"example.com", "127.0.0.1", "::1"})


async def test_dns_rebinding_cannot_change_validated_connection_ip(monkeypatch):
    lookup = AsyncMock(
        side_effect=[
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
        ]
    )
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    resolver = PublicResolver()
    first = await resolver.resolve("example.com", 443)
    assert first[0]["host"] == "93.184.216.34" and first[0]["flags"] == socket.AI_NUMERICHOST
    assert lookup.await_count == 1
    with pytest.raises(UnsafeSource):
        await resolver.resolve("example.com", 443)


async def test_mixed_public_private_dns_is_rejected(monkeypatch):
    lookup = AsyncMock(
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443)),
        ]
    )
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    with pytest.raises(UnsafeSource):
        await PublicResolver().resolve("example.com", 443)


def test_startup_errors_do_not_log_secrets():
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["ECHO_DATABASE_URL"] = "invalid://user:SECRET_DATABASE_PASSWORD@invalid/echo"
    env["ECHO_AI_KEY"] = "SECRET_AI_KEY"
    result = subprocess.run(
        [sys.executable, "-m", "echo.main", "health"], env=env, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 1
    logs = result.stdout + result.stderr
    assert "SECRET_DATABASE_PASSWORD" not in logs and "SECRET_AI_KEY" not in logs
    assert "startup_failed type=" in logs
