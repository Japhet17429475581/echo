import enum
import ipaddress
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


def now() -> datetime:
    return datetime.now(UTC)


class Status(enum.StrEnum):
    POSSIBLE_MATCH = "POSSIBLE_MATCH"
    NOT_FOUND = "NOT_FOUND"
    BLOCKED = "BLOCKED"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    SITE_ERROR = "SITE_ERROR"
    UNKNOWN = "UNKNOWN"


class JobState(enum.StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Category(enum.StrEnum):
    DEVELOPMENT = "development"
    GAMES = "games"
    EDUCATION = "education"
    CREATIVE = "creative"
    COMMUNITY = "community"
    MEDIA = "media"
    COMMERCE = "commerce"
    PROFESSIONAL = "professional"
    OTHER = "other"


class Engine(enum.StrEnum):
    SHERLOCK = "sherlock"
    MAIGRET = "maigret"


ACTIVE = (JobState.QUEUED, JobState.RUNNING)
ERRORS = {
    Status.BLOCKED,
    Status.RATE_LIMITED,
    Status.TIMEOUT,
    Status.NETWORK_ERROR,
    Status.SITE_ERROR,
    Status.UNKNOWN,
}
TRANSIENT = {Status.RATE_LIMITED, Status.TIMEOUT, Status.NETWORK_ERROR, Status.SITE_ERROR}
FALSE_POSITIVE_REASONS = {"generic_page", "different_username", "unavailable_profile", "other"}


def username(value: str) -> str:
    value = value.strip().removeprefix("@")
    if not re.fullmatch(r"[\w][\w.\-]{0,63}", value, re.UNICODE):
        raise ValueError("invalid_username")
    if value in {".", ".."} or any(ord(c) < 32 for c in value):
        raise ValueError("invalid_username")
    return value


def safe_url(value: str) -> str | None:
    try:
        p = urlsplit(value)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            return None
        if any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
            return None
        host = p.hostname.encode("idna").decode("ascii").lower()
        if host.endswith(".onion") or host == "onion" or host == "localhost":
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        port = p.port
        if ":" in host:
            host = f"[{host}]"
        if port and (p.scheme, port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        return urlunsplit((p.scheme, host, p.path or "/", p.query, p.fragment))
    except (ValueError, UnicodeError):
        return None


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: Engine
    version: str = Field(max_length=60)
    site_key: str = Field(min_length=1, max_length=200)
    site_name: str = Field(min_length=1, max_length=200)
    domain: str = Field(min_length=1, max_length=253)
    username: str = Field(min_length=1, max_length=64)
    url: str = Field(max_length=4096)
    raw_status: str = Field(max_length=120)
    status: Status = Status.UNKNOWN
    http_code: int | None = Field(None, ge=100, le=599)
    final_url: str | None = Field(None, max_length=4096)
    evidence: str | None = Field(None, max_length=500)
    error_type: str | None = Field(None, max_length=60)
    check_method: str | None = Field(None, max_length=80)
    redirect_kind: str | None = Field(None, max_length=30)
    retry_after: str | None = Field(None, max_length=120)
    checked_at: datetime = Field(default_factory=now)

    @field_validator("url")
    @classmethod
    def check_url(cls, value):
        if not safe_url(value):
            raise ValueError("unsafe_url")
        return value

    @field_validator("final_url")
    @classmethod
    def check_final(cls, value):
        return safe_url(value) if value else None


def normalize(o: Observation) -> Status:
    # Engine rules establish existence. HTTP only downgrades unreliable outcomes.
    if o.http_code == 403:
        return Status.BLOCKED
    if o.http_code == 429:
        return Status.RATE_LIMITED
    if o.http_code and o.http_code >= 500:
        return Status.SITE_ERROR
    if o.redirect_kind in {"home", "login", "search"}:
        return Status.UNKNOWN
    if o.error_type == "timeout":
        return Status.TIMEOUT
    if o.error_type in {"network", "dns", "connection"}:
        return Status.NETWORK_ERROR
    raw = o.raw_status.lower()
    if raw in {"claimed", "found"}:
        return Status.POSSIBLE_MATCH
    if raw in {"available", "not found"}:
        return Status.NOT_FOUND
    if raw == "illegal":
        return Status.UNKNOWN
    return Status.UNKNOWN


def merge_key(site_id: str, url: str) -> tuple[str, str]:
    return site_id, safe_url(url) or url


def disagreement(observations: list[dict]) -> bool:
    statuses = {o["status"] for o in observations}
    return len({o["engine"] for o in observations}) > 1 and len(statuses) > 1
