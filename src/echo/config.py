from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from echo.database import normalize_database_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_prefix="ECHO_")

    brand: str = Field(default="Echo", min_length=1, max_length=40)
    bot_token: SecretStr = SecretStr("")
    database_url: str = "postgresql+asyncpg://echo:echo@postgres/echo"
    redis_url: str = "redis://redis:6379/0"
    admins: str = ""
    timezone: str = "Europe/Moscow"
    temp_dir: Path = Path("/tmp/echo")
    sherlock_python: str = "/opt/sherlock/bin/python"
    maigret_python: str = "/opt/maigret/bin/python"
    engine_timeout: int = Field(900, ge=10, le=7200)
    site_timeout: int = Field(15, ge=1, le=60)
    engine_memory_mb: int = Field(768, ge=256)
    engine_output_mb: int = Field(32, ge=1, le=128)
    max_sites: int = Field(500, ge=1, le=5000)
    global_active_jobs: int = Field(1, ge=1, le=32)
    user_active_jobs: int = Field(1, ge=1, le=10)
    global_queued_jobs: int = Field(100, ge=1)
    user_daily_jobs: int = Field(20, ge=1)
    domain_interval: float = Field(1.0, ge=0.1)
    lease_seconds: int = Field(60, ge=15)
    max_deliveries: int = Field(3, ge=1, le=5)
    result_ttl_hours: int = Field(6, ge=1, le=168)
    history_ttl_days: int = Field(30, ge=1, le=365)
    progress_interval: int = Field(4, ge=2)
    cleanup_interval: int = Field(60, ge=5)
    telegram_file_mb: int = Field(45, ge=1, le=45)
    age_binary: str = "age"
    pdf_font: Path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")

    resource_profile: str = Field("eco", pattern="^(eco|standard)$")
    heavy_slots: int = Field(1, ge=1, le=4)
    sequential_engines: bool = True
    tools_python: str = "/opt/tools/bin/python"
    metadata_enabled: bool = True
    ocr_enabled: bool = True
    qr_enabled: bool = True
    passport_enabled: bool = True
    page_enabled: bool = True
    domains_enabled: bool = True
    metadata_timeout: int = Field(30, ge=2, le=180)
    ocr_timeout: int = Field(90, ge=2, le=300)
    qr_timeout: int = Field(30, ge=2, le=120)
    passport_timeout: int = Field(60, ge=5, le=180)
    page_timeout: int = Field(45, ge=5, le=120)
    domains_timeout: int = Field(120, ge=5, le=300)
    metadata_memory_mb: int = Field(384, ge=128, le=2048)
    ocr_memory_mb: int = Field(768, ge=256, le=2048)
    qr_memory_mb: int = Field(384, ge=128, le=2048)
    passport_memory_mb: int = Field(512, ge=128, le=2048)
    page_memory_mb: int = Field(512, ge=128, le=2048)
    domains_memory_mb: int = Field(512, ge=128, le=2048)
    tools_cpu_seconds: int = Field(90, ge=2, le=300)
    tools_processes: int = Field(16, ge=4, le=64)
    tools_output_bytes: int = Field(524288, ge=10000, le=2097152)
    file_max_mb: int = Field(10, ge=1, le=20)
    image_max_pixels: int = Field(8_000_000, ge=10000, le=25_000_000)
    image_max_pages: int = Field(1, ge=1, le=1)
    ocr_threads: int = Field(1, ge=1, le=4)
    net_connections: int = Field(2, ge=1, le=4)
    net_timeout: int = Field(10, ge=2, le=30)
    net_response_bytes: int = Field(1_000_000, ge=10000, le=5_000_000)
    net_header_bytes: int = Field(32768, ge=4096, le=65536)
    net_redirects: int = Field(3, ge=0, le=5)
    dns_timeout: float = Field(2, ge=0.1, le=5)
    dns_record_limit: int = Field(8, ge=1, le=30)
    domain_variants: int = Field(25, ge=1, le=100)
    domain_dns_queries: int = Field(50, ge=0, le=200)
    data_dir: Path = Path("/data/echo")
    disk_quota_mb: int = Field(256, ge=32, le=16384)
    user_disk_quota_mb: int = Field(64, ge=10, le=1024)
    disk_min_free_mb: int = Field(64, ge=1)
    scratch_quota_mb: int = Field(64, ge=16, le=256)
    folder_ttl_days: int = Field(30, ge=1, le=365)
    max_folders: int = Field(10, ge=1, le=100)
    max_folder_items: int = Field(50, ge=1, le=500)
    export_timeout: int = Field(120, ge=10, le=600)
    export_memory_mb: int = Field(768, ge=256, le=2048)
    export_max_mb: int = Field(40, ge=1, le=40)
    brand_images: bool = True
    recent_result_minutes: int = Field(30, ge=1, le=1440)
    batch_max_cards: int = Field(50, ge=1, le=100)

    ai_enabled: bool = False
    ai_intents: bool = False
    ai_endpoint: str = "https://api.deepseek.com"
    ai_model: str = "deepseek-flash"
    ai_key: SecretStr = SecretStr("")
    ai_timeout: float = Field(20, ge=1, le=120)
    ai_max_input_chars: int = Field(6000, ge=1000, le=12000)
    ai_max_output_tokens: int = Field(400, ge=100, le=1000)
    ai_parallel: int = Field(2, ge=1, le=16)
    ai_user_daily_requests: int = Field(30, ge=0)
    ai_daily_requests: int = Field(300, ge=0)
    ai_description_daily: int = Field(200, ge=0)
    ai_translation_daily: int = Field(100, ge=0)
    ai_explanation_daily: int = Field(50, ge=0)
    ai_intent_daily: int = Field(50, ge=0)
    ai_daily_budget_usd: float = Field(1, ge=0)
    ai_input_usd_million: float | None = Field(None, ge=0)
    ai_output_usd_million: float | None = Field(None, ge=0)
    ai_price_date: str | None = None
    ai_retries: int = Field(1, ge=0, le=2)
    ai_retry_max_delay: float = Field(15, ge=0, le=60)
    ai_breaker_failures: int = Field(4, ge=1)
    ai_breaker_seconds: int = Field(120, ge=10)
    catalog_ttl_days: int = Field(30, ge=1)
    catalog_negative_ttl_hours: int = Field(6, ge=1)
    catalog_lock_seconds: int = Field(180, ge=30)
    source_max_bytes: int = Field(200_000, ge=1000, le=1_000_000)

    @field_validator("database_url")
    @classmethod
    def valid_database_url(cls, value: str) -> str:
        return normalize_database_url(value)

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def valid_pricing(self):
        if self.resource_profile == "eco" and (
            self.heavy_slots != 1
            or self.global_active_jobs != 1
            or self.user_active_jobs != 1
            or not self.sequential_engines
            or self.ocr_threads != 1
        ):
            raise ValueError("eco requires one global task/heavy slot, sequential engines and one OCR thread")
        if (self.ai_input_usd_million is None) != (self.ai_output_usd_million is None):
            raise ValueError("Configure both input and output prices")
        if self.ai_input_usd_million is not None and not self.ai_price_date:
            raise ValueError("AI price date is required with prices")
        return self

    @property
    def admin_ids(self) -> set[int]:
        return {int(v.strip()) for v in self.admins.split(",") if v.strip()}


@lru_cache
def settings() -> Settings:
    return Settings()
