"""JSON-lines bridge to pinned upstream libraries. No shell, CLI-output parsing or profile extraction."""

import asyncio
import contextlib
import importlib.metadata
import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

WIRE = sys.stdout


def emit(kind, **fields):
    WIRE.write(json.dumps({"type": kind, **fields}, ensure_ascii=False) + "\n")
    WIRE.flush()


def safe_endpoint(url):
    import ipaddress

    try:
        p = urlsplit(url.replace("{}", "echoexample"))
        host = p.hostname or ""
        if (
            p.scheme not in {"http", "https"}
            or not host
            or p.username
            or p.password
            or host.endswith((".onion", ".i2p", ".local", ".localhost"))
            or host in {"localhost", "onion"}
            or "\\" in url
        ):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host
    except ValueError:
        return False


def error_kind(text):
    text = str(text or "").lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if any(s in text for s in ("captcha", "waf", "access denied", "bot protection", "blocked")):
        return "blocked"
    if any(s in text for s in ("connecting", "connection", "dns", "resolving", "network")):
        return "network"
    return "engine_error" if text else None


def serialize(engine, version, name, spec, result, nick):
    status = result.get("status")
    raw = str(status.status) if status else "Unknown"
    url = result.get("url_user") or getattr(status, "site_url_user", "")
    code = result.get("http_status")
    err = getattr(status, "error", None)
    context = getattr(status, "context", None)
    method = spec.get("errorType") if isinstance(spec, dict) else spec.check_type
    if isinstance(method, list):
        method = ",".join(method)
    return {
        "engine": engine,
        "version": version,
        "site_key": name,
        "site_name": name,
        "domain": urlsplit(url).hostname or urlsplit(result.get("url_main", "")).hostname or "unknown",
        "username": nick,
        "url": url,
        "raw_status": raw,
        "http_code": code if isinstance(code, int) and 100 <= code <= 599 else None,
        "final_url": result.get("_echo_final_url"),
        "evidence": None,
        "redirect_kind": result.get("_echo_redirect_kind"),
        "retry_after": result.get("_echo_retry_after"),
        "check_method": str(method)[:80] if method else None,
        "error_type": error_kind(getattr(err, "type", None) or context),
    }


def sherlock_sites():
    import sherlock_project

    return json.loads((Path(sherlock_project.__file__).parent / "resources/data.json").read_text("utf-8"))


def maigret_sites():
    import maigret
    from maigret.sites import MaigretDatabase

    db = MaigretDatabase().load_from_path(str(Path(maigret.__file__).parent / "resources/data.json"))
    return db.ranked_sites_dict(disabled=False, excluded_tags=["tor", "i2p"], top=100000)


def select_sites(engine, sites, request):
    curated = {
        "github.com",
        "gitlab.com",
        "stackoverflow.com",
        "reddit.com",
        "twitch.tv",
        "steamcommunity.com",
        "youtube.com",
        "pinterest.com",
        "medium.com",
        "dev.to",
        "codeberg.org",
        "lichess.org",
        "chess.com",
        "keybase.io",
        "soundcloud.com",
    }
    selected = {}
    for name, site in sites.items():
        if engine == "sherlock":
            if not isinstance(site, dict) or "url" not in site:
                continue
            main, url = site.get("urlMain", ""), site["url"]
            probe = site.get("urlProbe", url)
        else:
            main, url, probe = site.url_main, site.url, site.url_probe or site.url
            # No third-party mirrors, activation tokens, Tor, fuzzy matches or bypass transports.
            if (
                site.protocol
                or site.disabled
                or site.source
                or site.similar_search
                or site.activation
                or "tls_fingerprint" in site.protection
                or getattr(site, "request_head_only", False)
                and not url
            ):
                continue
        if not all(safe_endpoint(u) for u in (main, url, probe)):
            continue
        domain = (urlsplit(main).hostname or "").removeprefix("www.")
        if request["coverage"] == "curated" and domain not in curated:
            continue
        targets = request.get("targets")
        if targets is not None and name not in targets:
            continue
        selected[name] = site
    if request.get("targets") is not None:
        missing = set(request["targets"]) - selected.keys()
        if missing:
            emit("warning", code="targets_unavailable", count=len(missing))
    limit = 100 if request["coverage"] == "top100" else request["max_sites"]
    # Sherlock has no popularity ranking: top100 means first 100 eligible manifest entries.
    return dict(list(selected.items())[:limit])


def check_sherlock(nick, sites, timeout):
    from sherlock_project.notify import QueryNotify
    from sherlock_project.sherlock import sherlock

    results = sherlock(nick, sites, QueryNotify(), timeout=timeout)
    for name, spec in sites.items():
        try:
            response = spec["request_future"].result()
            results[name]["_echo_final_url"] = response.url
            results[name]["_echo_retry_after"] = response.headers.get("Retry-After")
            target = urljoin(response.url, response.headers.get("Location", ""))
            results[name]["_echo_redirect_kind"] = redirect_kind(
                target if 300 <= response.status_code < 400 else response.url,
                bool(response.history) or 300 <= response.status_code < 400,
            )
        except Exception:
            pass  # no fabricated response metadata when upstream did not obtain a response
    return results


async def check_maigret(nick, sites, site_timeout):
    import aiohttp
    import maigret.checking as checking

    original = checking.SimpleAiohttpChecker

    class CapturingChecker(original):
        async def check(self):
            trace = aiohttp.TraceConfig()

            async def response_end(session, context, params):
                response = params.response
                self.echo_final_url = str(response.url)
                self.echo_retry_after = response.headers.get("Retry-After")
                target = urljoin(str(response.url), response.headers.get("Location", ""))
                self.echo_redirect_kind = redirect_kind(
                    target if 300 <= response.status < 400 else str(response.url),
                    bool(response.history) or 300 <= response.status < 400,
                )

            trace.on_request_end.append(response_end)
            # Preserve engine rules; use verified TLS and no profile cookies or environment proxies.
            async with aiohttp.ClientSession(
                trace_configs=[trace],
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
                connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver()),
            ) as session:
                return await self._make_request(
                    session,
                    self.url,
                    self.headers,
                    self.allow_redirects,
                    self.timeout,
                    self.method,
                    self.logger,
                    self.payload,
                )

    checking.SimpleAiohttpChecker = CapturingChecker
    try:
        results = await checking.maigret(
            nick,
            sites,
            logging.getLogger("disabled"),
            timeout=site_timeout,
            is_parsing_enabled=False,
            is_enrich_enabled=False,
            retries=0,
            check_domains=False,
            max_connections=1,
            no_progressbar=True,
            cloudflare_bypass=None,
            dns_resolver="threaded",
        )
        for result in results.values():
            checker = result.get("checker")
            result["_echo_final_url"] = getattr(checker, "echo_final_url", None)
            result["_echo_retry_after"] = getattr(checker, "echo_retry_after", None)
            result["_echo_redirect_kind"] = getattr(checker, "echo_redirect_kind", None)
        return results
    finally:
        checking.SimpleAiohttpChecker = original


def redirect_kind(url, redirected):
    if not redirected:
        return None
    path = urlsplit(url).path.lower().rstrip("/")
    if not path:
        return "home"
    if path.split("/")[-1] in {"login", "signin", "sign-in", "auth", "log-in"}:
        return "login"
    if path.split("/")[-1] in {"search", "search.php"}:
        return "search"
    return None


async def run(request, engine):
    version = importlib.metadata.version("sherlock-project" if engine == "sherlock" else "maigret")
    sites = select_sites(engine, sherlock_sites() if engine == "sherlock" else maigret_sites(), request)
    sites = {k: v for k, v in sites.items() if k not in request.get("skip", [])}
    emit("ready", engine=engine, version=version, total=len(sites))
    sink = open(os.devnull, "w")  # noqa: ASYNC230 -- one process-local diagnostic sink, no user files
    for name, spec in sites.items():
        urls = (
            [spec.get("urlMain"), spec.get("urlProbe") or spec["url"]]
            if engine == "sherlock"
            else [spec.url_main, spec.url_probe or spec.url]
        )
        emit("permit", domains=sorted({urlsplit(u).hostname for u in urls if u}))
        # Parent grants a distributed domain slot; cancellation closes/kills the child.
        if not sys.stdin.readline():
            return
        started = time.monotonic()
        with contextlib.redirect_stdout(sink):
            if engine == "sherlock":
                results = check_sherlock(request["username"], {name: spec}, request["timeout"])
            else:
                results = await check_maigret(request["username"], {name: spec}, request["timeout"])
        for key, result in results.items():
            data = serialize(engine, version, key, spec, result, request["username"])
            if safe_endpoint(data["url"]):
                emit("result", data=data, elapsed_ms=int((time.monotonic() - started) * 1000))
    emit("done")
    sink.close()


def main():
    logging.disable(logging.CRITICAL)
    request = json.loads(sys.stdin.readline())
    if os.name == "posix":
        import resource

        memory = request["memory_mb"] * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        asyncio.run(run(request, sys.argv[1]))
    except Exception:
        emit("fatal", code="engine_failure")
        sys.exit(1)


if __name__ == "__main__":
    main()
