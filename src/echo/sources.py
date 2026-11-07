import asyncio
import hashlib
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from bs4 import BeautifulSoup

from echo.domain import safe_url


class UnsafeSource(ValueError):
    pass


def public_ip(value):
    ip = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def source_url(value, allowed_hosts):
    try:
        p = urlsplit(value)
        host = (p.hostname or "").encode("idna").decode().lower()
        if (
            p.scheme not in {"http", "https"}
            or not host
            or p.username
            or p.password
            or host not in allowed_hosts
            or p.port not in {None, 80, 443}
            or "\\" in value
            or any(ord(c) < 33 for c in value)
            or host.endswith((".onion", ".local", ".localhost"))
        ):
            raise UnsafeSource()
        try:
            if not public_ip(host):
                raise UnsafeSource()
        except ValueError as e:
            if isinstance(e, UnsafeSource):
                raise
        return value
    except (ValueError, UnicodeError) as e:
        raise UnsafeSource() from e


class PublicResolver(AbstractResolver):
    """DNS is resolved once, checked, then those exact addresses go to TCPConnector."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM, family=socket.AF_UNSPEC
        )
        addresses = {info[4][0] for info in infos}
        if not addresses or not all(public_ip(a) for a in addresses):
            raise UnsafeSource("non_public_dns")
        return [
            {
                "hostname": host,
                "host": a,
                "port": port,
                "family": socket.AF_INET6 if ":" in a else socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
            for a in sorted(addresses)
        ]

    async def close(self):
        pass


async def fetch_official(url, domains, maximum=200_000):
    allowed = {d.lower() for d in domains}
    allowed |= {"www." + d for d in list(allowed) if not d.startswith("www.")}
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False, limit=2)
    async with (
        asyncio.timeout(20),
        aiohttp.ClientSession(
            connector=connector,
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
            auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "Echo-SiteCatalog/1.0", "Accept-Encoding": "identity"},
        ) as client,
    ):
        for _ in range(4):
            source_url(url, allowed)
            async with client.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise UnsafeSource("empty_redirect")
                    url = urljoin(url, location)
                    continue
                if (
                    response.status != 200
                    or response.headers.get("Content-Encoding", "identity") != "identity"
                ):
                    raise UnsafeSource("source_unavailable")
                if response.content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
                    raise UnsafeSource("content_type")
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body) > maximum:
                        raise UnsafeSource("source_too_large")
                soup = BeautifulSoup(bytes(body), "html.parser")
                for node in soup(["script", "style", "noscript", "nav", "footer", "form", "iframe"]):
                    node.decompose()
                meta = soup.find("meta", attrs={"name": "description"})
                description = str(meta.get("content", "")) if meta else ""
                text = " ".join((description + " " + soup.get_text(" ", strip=True)).split())[:1800]
                if len(text) < 40:
                    raise UnsafeSource("insufficient_source")
                return text, url
        raise UnsafeSource("redirect_limit")


def public_target(value):
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    normalized = safe_url(value)
    if not normalized or len(normalized) > 4096:
        raise UnsafeSource("invalid_url")
    p = urlsplit(normalized)
    if p.port not in {None, 443 if p.scheme == "https" else 80}:
        raise UnsafeSource("invalid_port")
    host = p.hostname
    if (
        not host
        or "." not in host
        and ":" not in host
        or host.endswith((".local", ".internal", ".localhost", ".onion", ".i2p"))
    ):
        raise UnsafeSource("non_public_host")
    # PublicResolver pins every actual socket address, including numeric forms.
    return normalized


@dataclass
class WebResponse:
    original_url: str
    final_url: str
    status: int
    redirects: list
    headers: dict
    body: bytes
    truncated: bool = False


class SafeNetwork:
    """Shared HTTP policy for user-facing modules; never forwards credentials/cookies."""

    def __init__(self, config, limiter=None, resolver=None):
        self.config, self.limiter = config, limiter
        self.resolver = resolver or PublicResolver()

    async def fetch(self, value, *, truncate=False, allowed_hosts=None, maximum=None):
        original = url = public_target(value)
        maximum = min(maximum or self.config.net_response_bytes, self.config.net_response_bytes)
        connector = aiohttp.TCPConnector(
            resolver=self.resolver, use_dns_cache=False, limit=self.config.net_connections
        )
        async with (
            asyncio.timeout(self.config.net_timeout * (self.config.net_redirects + 1)),
            aiohttp.ClientSession(
                connector=connector,
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
                auto_decompress=False,
                timeout=aiohttp.ClientTimeout(total=self.config.net_timeout),
                max_line_size=4096,
                max_field_size=4096,
                headers={"User-Agent": "Echo-Tools/1.1", "Accept-Encoding": "identity"},
            ) as client,
        ):
            chain = []
            for hop in range(self.config.net_redirects + 1):
                url = public_target(url)
                host = urlsplit(url).hostname
                if allowed_hosts is not None and host not in allowed_hosts:
                    raise UnsafeSource("redirect_host")
                if self.limiter:
                    await self.limiter.wait([host])
                async with client.get(url, allow_redirects=False) as response:
                    if (
                        sum(len(k) + len(v) + 4 for k, v in response.raw_headers)
                        > self.config.net_header_bytes
                    ):
                        raise UnsafeSource("header_limit")
                    if response.status in {301, 302, 303, 307, 308}:
                        if hop == self.config.net_redirects:
                            raise UnsafeSource("redirect_limit")
                        location = response.headers.get("Location")
                        if not location:
                            raise UnsafeSource("empty_redirect")
                        following = public_target(urljoin(url, location))
                        chain.append({"url": url, "status": response.status, "next_url": following})
                        url = following
                        continue
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise UnsafeSource("compressed_response")
                    headers = {
                        k.lower(): v[:2048]
                        for k, v in response.headers.items()
                        if k.lower()
                        in {
                            "content-type",
                            "server",
                            "x-powered-by",
                            "strict-transport-security",
                            "last-modified",
                        }
                    }
                    body = bytearray()
                    cut = False
                    async for chunk in response.content.iter_chunked(8192):
                        if len(body) + len(chunk) > maximum:
                            if not truncate:
                                raise UnsafeSource("response_limit")
                            body.extend(chunk[: maximum - len(body)])
                            cut = True
                            break
                        body.extend(chunk)
                    return WebResponse(original, url, response.status, chain, headers, bytes(body), cut)
            raise UnsafeSource("redirect_limit")

    async def tls(self, domain):
        addresses = await self.resolver.resolve(domain, 443)
        if not addresses:
            raise UnsafeSource("non_public_dns")
        ip = addresses[0]["host"]
        if not public_ip(ip):
            raise UnsafeSource("non_public_dns")
        async with asyncio.timeout(self.config.net_timeout):
            _, writer = await asyncio.open_connection(
                ip, 443, ssl=ssl.create_default_context(), server_hostname=domain
            )
            try:
                session = writer.get_extra_info("ssl_object")
                cert = session.getpeercert()
                return {
                    "verified": True,
                    "subject": cert.get("subject"),
                    "issuer": cert.get("issuer"),
                    "not_before": cert.get("notBefore"),
                    "not_after": cert.get("notAfter"),
                    "serial_number": cert.get("serialNumber"),
                    "subject_alt_names": cert.get("subjectAltName", [])[:100],
                    "sha256": hashlib.sha256(session.getpeercert(binary_form=True)).hexdigest(),
                    "protocol": session.version(),
                    "cipher": session.cipher(),
                }
            finally:
                writer.close()
                await writer.wait_closed()
