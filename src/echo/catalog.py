import hashlib
import json
import uuid
from datetime import timedelta
from importlib.resources import files
from urllib.parse import urlsplit

import tldextract
from sqlalchemy import select

from echo.ai import PROMPT_VERSION, UNLOCK, Description, Operation, Unavailable
from echo.domain import now
from echo.services import as_utc
from echo.sources import fetch_official
from echo.storage import Gate, Site

PSL = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True, cache_dir=None)


def identity(domain: str, site_key: str, known: dict[str, str]) -> str:
    host = domain.encode("idna").decode().lower()
    # Only explicitly reviewed aliases merge domains; PSL is never used to collapse all tenants.
    if host in known:
        return known[host]
    extracted = PSL(host)
    if not extracted.suffix:
        return host + ":" + hashlib.sha256(site_key.lower().encode()).hexdigest()[:12]
    # Keep the whole hostname, including private-suffix tenant components.
    # URL equality also preserves paths on multi-tenant, path-based hosts.
    return host


async def seed_catalog(sessions):
    seeds = json.loads(files("echo.data").joinpath("sites.json").read_text("utf-8"))
    async with sessions.begin() as s:
        await s.get(Gate, 1, with_for_update=True)
        for seed in seeds:
            for lang in ("ru", "en"):
                existing = await s.get(Site, (seed["id"], lang))
                if existing:
                    continue
                description = seed[lang]
                s.add(
                    Site(
                        id=seed["id"],
                        language=lang,
                        name=seed["name"],
                        domains=seed["domains"],
                        category=seed["category"],
                        description=description["description"],
                        activities=description["activities"],
                        sources=seed["sources"],
                        origin="human",
                        reviewed="human_reviewed",
                        schema_version=1,
                        prompt_version=PROMPT_VERSION,
                        checked_at=now(),
                        refresh_at=now() + timedelta(days=3650),
                        source_hash=hashlib.sha256(
                            json.dumps(description, sort_keys=True).encode()
                        ).hexdigest(),
                    )
                )


class Catalog:
    def __init__(self, sessions, redis, gateway, config):
        self.sessions, self.redis, self.gateway, self.config = sessions, redis, gateway, config

    async def aliases(self):
        async with self.sessions() as s:
            records = await s.scalars(select(Site).where(Site.language == "en"))
            return {d: r.id for r in records for d in r.domains}

    async def get(self, site_id, language, scope, *, refresh=False, guard=None):
        async with self.sessions() as s:
            site = await s.get(Site, (site_id, language))
            translations = await s.scalars(select(Site).where(Site.id == site_id))
            original = next((r for r in translations if r.description and r.origin == "human"), None)
        compatible = bool(site and site.schema_version == 1 and site.prompt_version == PROMPT_VERSION)
        if (
            site
            and (site.origin == "human" or compatible and as_utc(site.refresh_at) > now())
            and not refresh
        ):
            await self.redis.incr("echo:ai:cache_hit")
            return site
        cooldown = f"echo:catalog:cooldown:{site_id}:{language}:1:{PROMPT_VERSION}"
        if not refresh and await self.redis.exists(cooldown):
            return site
        if guard is not None and not await guard():
            return site
        # Stale while revalidate: caller reads existing DB immediately; only enrichment does the refresh.
        lock = f"echo:catalog:lock:{site_id}:{language}:1:{PROMPT_VERSION}"
        token = str(uuid.uuid4())
        lock_ttl = max(
            self.config.catalog_lock_seconds,
            int(
                2 * (self.config.ai_retries + 1) * (self.config.ai_timeout + self.config.ai_retry_max_delay)
                + 90
            ),
        )
        if not await self.redis.set(lock, token, nx=True, ex=lock_ttl):
            return site
        try:
            # Recheck after acquiring: concurrent workers may have just committed.
            async with self.sessions() as s:
                latest = await s.get(Site, (site_id, language))
                compatible = bool(
                    latest and latest.schema_version == 1 and latest.prompt_version == PROMPT_VERSION
                )
                if (
                    latest
                    and (latest.origin == "human" or compatible and as_utc(latest.refresh_at) > now())
                    and not refresh
                ):
                    return latest
            if latest and latest.origin == "human":
                return latest
            await self.redis.incr("echo:ai:cache_miss")
            if original and original.language != language:
                data = {
                    "site_name": original.name,
                    "description": original.description,
                    "category": original.category,
                    "typical_activities": original.activities,
                }
                result = await self.gateway.generate(
                    Operation.TRANSLATION, data, language, scope, Description, guard=guard
                )
                sources, origin = original.sources, "ai_translation:human"
                source_hash = original.source_hash
                name, domains = original.name, original.domains
            elif site and site.sources:
                # Only catalogue-approved official source URLs are fetched, never a profile URL.
                text, source = await fetch_official(
                    site.sources[0], site.domains, self.config.source_max_bytes
                )
                name, domains = site.name, site.domains
                data = {
                    "site_name": name,
                    "domain": urlsplit(source).hostname,
                    "service_excerpt": text,
                    "language": language,
                }
                source_hash = hashlib.sha256(text.encode()).hexdigest()
                result = await self.gateway.generate(
                    Operation.DESCRIPTION, data, language, scope, Description, guard=guard
                )
                sources, origin = [source], "ai_source"
            else:
                # Unknown domains cannot be assumed to be official service descriptions.
                return site
            if result.insufficient_evidence:
                return await self.negative(site_id, language, name, domains, sources)
            async with self.sessions.begin() as s:
                entry = await s.get(Site, (site_id, language), with_for_update=True)
                if entry and entry.origin == "human":
                    return entry
                if not entry:
                    entry = Site(id=site_id, language=language)
                    s.add(entry)
                entry.name, entry.domains = name, domains
                entry.description, entry.activities = result.description, result.typical_activities
                entry.category, entry.sources, entry.origin = result.category, sources, origin
                entry.reviewed, entry.source_hash = "source_based", source_hash
                entry.checked_at, entry.refresh_at = (
                    now(),
                    now() + timedelta(days=self.config.catalog_ttl_days),
                )
                entry.schema_version, entry.prompt_version = 1, PROMPT_VERSION
                await s.flush()
                return entry
        except (Unavailable, ValueError, OSError, TimeoutError):
            if site and site.description:
                await self.redis.set(cooldown, "1", ex=self.config.catalog_negative_ttl_hours * 3600)
                # Keep stale acceptable text and throttle refresh attempts.
                async with self.sessions.begin() as s:
                    entry = await s.get(Site, (site_id, language), with_for_update=True)
                    if entry and entry.origin != "human":
                        entry.refresh_at = now() + timedelta(hours=self.config.catalog_negative_ttl_hours)
                return site
            if site:
                return await self.negative(site_id, language, site.name, site.domains, site.sources)
            if original:
                return await self.negative(
                    site_id, language, original.name, original.domains, original.sources
                )
            return None
        finally:
            await self.redis.eval(UNLOCK, 1, lock, token)

    async def negative(self, site_id, language, name, domains, sources):
        async with self.sessions.begin() as s:
            entry = await s.get(Site, (site_id, language), with_for_update=True)
            if entry and entry.origin == "human":
                return entry
            if not entry:
                entry = Site(id=site_id, language=language)
                s.add(entry)
            entry.name, entry.domains, entry.sources = name, domains, sources
            entry.origin, entry.description, entry.reviewed = "unavailable", "", "unavailable"
            entry.category, entry.activities = "other", []
            entry.checked_at = now()
            entry.schema_version, entry.prompt_version = 1, PROMPT_VERSION
            entry.refresh_at = now() + timedelta(hours=self.config.catalog_negative_ttl_hours)
            await s.flush()
            return entry
