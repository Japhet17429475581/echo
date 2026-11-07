import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from echo.domain import now


class Base(DeclarativeBase):
    pass


class Gate(Base):
    __tablename__ = "queue_gate"
    id: Mapped[int] = mapped_column(primary_key=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    language: Mapped[str] = mapped_column(String(2), default="ru")
    history: Mapped[bool] = mapped_column(Boolean, default=False)
    ai: Mapped[bool] = mapped_column(Boolean, default=False)
    recipient: Mapped[str | None] = mapped_column(String(120))
    generation: Mapped[str] = mapped_column(String(36), default=lambda: str(uuid.uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    generation: Mapped[str] = mapped_column(String(36))
    request_key: Mapped[str] = mapped_column(String(100), unique=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    username: Mapped[str] = mapped_column(String(64))
    engines: Mapped[list] = mapped_column(JSON)
    coverage: Mapped[str] = mapped_column(String(20))
    ai: Mapped[bool] = mapped_column(Boolean, default=False)
    targets: Mapped[dict | None] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    enrichment: Mapped[str] = mapped_column(String(20), default="DISABLED")
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    failures: Mapped[dict] = mapped_column(JSON, default=dict)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    lease: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(24), default="search", server_default="search", index=True)
    module_id: Mapped[str | None] = mapped_column(String(24))
    parameters: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    result: Mapped[dict | None] = mapped_column(JSON)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    ui_stage: Mapped[str] = mapped_column(String(20), default="", server_default="")


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class WorkspaceItem(Base):
    __tablename__ = "workspace_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    folder_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(120))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FileObject(Base):
    __tablename__ = "file_objects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    generation: Mapped[str] = mapped_column(String(36))
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    folder_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    storage_key: Mapped[str] = mapped_column(String(36), unique=True)
    mime: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    role: Mapped[str] = mapped_column(String(20), default="input")
    state: Mapped[str] = mapped_column(String(20), default="UPLOADING")
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Check(Base):
    __tablename__ = "checks"
    __table_args__ = (
        Index("check_job_profile", "job_id", "profile_key"),
        Index("check_engine_site", "job_id", "engine", "site_key", unique=True),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    profile_key: Mapped[str] = mapped_column(String(64))
    site_id: Mapped[str] = mapped_column(String(253))
    engine: Mapped[str] = mapped_column(String(20))
    site_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30))
    data: Mapped[dict] = mapped_column(JSON)


class Annotation(Base):
    __tablename__ = "annotations"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    profile_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    mark: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(Text, default="")
    false_positive_reason: Mapped[str | None] = mapped_column(String(32))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Site(Base):
    __tablename__ = "sites"
    id: Mapped[str] = mapped_column(String(253), primary_key=True)
    language: Mapped[str] = mapped_column(String(2), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    domains: Mapped[list] = mapped_column(JSON)
    category: Mapped[str] = mapped_column(String(30), default="other")
    description: Mapped[str] = mapped_column(Text, default="")
    activities: Mapped[list] = mapped_column(JSON, default=list)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(30), default="unavailable")
    reviewed: Mapped[str] = mapped_column(String(30), default="unavailable")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    refresh_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    prompt_version: Mapped[str] = mapped_column(String(20), default="1")
    source_hash: Mapped[str | None] = mapped_column(String(64))


class AICall(Base):
    __tablename__ = "ai_calls"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    operation: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20))
    attempts: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[str | None] = mapped_column(String(40))
    estimated: Mapped[bool] = mapped_column(Boolean, default=True)
    elapsed_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def session_factory(url: str):
    engine = create_async_engine(url, pool_pre_ping=True, hide_parameters=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
