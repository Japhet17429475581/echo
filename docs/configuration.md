# Configuration and limits

All application variables use the ECHO_ prefix. Changes require restarting bot/worker. Pydantic validates bounds at startup. POSTGRES_PASSWORD is consumed by Compose and must be a long random alphanumeric password.

Daily budgets use UTC, independently of the report timezone. Timeouts are seconds; size and memory limits are MiB. An unset price means unknown monetary cost; hard request and output limits remain active. Set both prices and their verification date together.

| Variable | Default | Bounds |
|---|---|---|
| `ECHO_BRAND` | `Echo` | MinLen(min_length=1), MaxLen(max_length=40) |
| `ECHO_BOT_TOKEN` | `unset (secret)` | none |
| `ECHO_DATABASE_URL` | `postgresql+asyncpg://echo:echo@postgres/echo` | none |
| `ECHO_REDIS_URL` | `redis://redis:6379/0` | none |
| `ECHO_ADMINS` | `` | none |
| `ECHO_TIMEZONE` | `Europe/Moscow` | none |
| `ECHO_TEMP_DIR` | `/tmp/echo` | none |
| `ECHO_SHERLOCK_PYTHON` | `/opt/sherlock/bin/python` | none |
| `ECHO_MAIGRET_PYTHON` | `/opt/maigret/bin/python` | none |
| `ECHO_ENGINE_TIMEOUT` | `900` | Ge(ge=10), Le(le=7200) |
| `ECHO_SITE_TIMEOUT` | `15` | Ge(ge=1), Le(le=60) |
| `ECHO_ENGINE_MEMORY_MB` | `768` | Ge(ge=256) |
| `ECHO_ENGINE_OUTPUT_MB` | `32` | Ge(ge=1), Le(le=128) |
| `ECHO_MAX_SITES` | `500` | Ge(ge=1), Le(le=5000) |
| `ECHO_GLOBAL_ACTIVE_JOBS` | `1` | Ge(ge=1), Le(le=32) |
| `ECHO_USER_ACTIVE_JOBS` | `1` | Ge(ge=1), Le(le=10) |
| `ECHO_GLOBAL_QUEUED_JOBS` | `100` | Ge(ge=1) |
| `ECHO_USER_DAILY_JOBS` | `20` | Ge(ge=1) |
| `ECHO_DOMAIN_INTERVAL` | `1.0` | Ge(ge=0.1) |
| `ECHO_LEASE_SECONDS` | `60` | Ge(ge=15) |
| `ECHO_MAX_DELIVERIES` | `3` | Ge(ge=1), Le(le=5) |
| `ECHO_RESULT_TTL_HOURS` | `6` | Ge(ge=1), Le(le=168) |
| `ECHO_HISTORY_TTL_DAYS` | `30` | Ge(ge=1), Le(le=365) |
| `ECHO_PROGRESS_INTERVAL` | `4` | Ge(ge=2) |
| `ECHO_CLEANUP_INTERVAL` | `60` | Ge(ge=5) |
| `ECHO_TELEGRAM_FILE_MB` | `45` | Ge(ge=1), Le(le=45) |
| `ECHO_AGE_BINARY` | `age` | none |
| `ECHO_PDF_FONT` | `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` | none |
| `ECHO_RESOURCE_PROFILE` | `eco` | pattern='^(eco|standard)$' |
| `ECHO_HEAVY_SLOTS` | `1` | Ge(ge=1), Le(le=4) |
| `ECHO_SEQUENTIAL_ENGINES` | `True` | none |
| `ECHO_TOOLS_PYTHON` | `/opt/tools/bin/python` | none |
| `ECHO_METADATA_ENABLED` | `True` | none |
| `ECHO_OCR_ENABLED` | `True` | none |
| `ECHO_QR_ENABLED` | `True` | none |
| `ECHO_PASSPORT_ENABLED` | `True` | none |
| `ECHO_PAGE_ENABLED` | `True` | none |
| `ECHO_DOMAINS_ENABLED` | `True` | none |
| `ECHO_METADATA_TIMEOUT` | `30` | Ge(ge=2), Le(le=180) |
| `ECHO_OCR_TIMEOUT` | `90` | Ge(ge=2), Le(le=300) |
| `ECHO_QR_TIMEOUT` | `30` | Ge(ge=2), Le(le=120) |
| `ECHO_PASSPORT_TIMEOUT` | `60` | Ge(ge=5), Le(le=180) |
| `ECHO_PAGE_TIMEOUT` | `45` | Ge(ge=5), Le(le=120) |
| `ECHO_DOMAINS_TIMEOUT` | `120` | Ge(ge=5), Le(le=300) |
| `ECHO_METADATA_MEMORY_MB` | `384` | Ge(ge=128), Le(le=2048) |
| `ECHO_OCR_MEMORY_MB` | `768` | Ge(ge=256), Le(le=2048) |
| `ECHO_QR_MEMORY_MB` | `384` | Ge(ge=128), Le(le=2048) |
| `ECHO_PASSPORT_MEMORY_MB` | `512` | Ge(ge=128), Le(le=2048) |
| `ECHO_PAGE_MEMORY_MB` | `512` | Ge(ge=128), Le(le=2048) |
| `ECHO_DOMAINS_MEMORY_MB` | `512` | Ge(ge=128), Le(le=2048) |
| `ECHO_TOOLS_CPU_SECONDS` | `90` | Ge(ge=2), Le(le=300) |
| `ECHO_TOOLS_PROCESSES` | `16` | Ge(ge=4), Le(le=64) |
| `ECHO_TOOLS_OUTPUT_BYTES` | `524288` | Ge(ge=10000), Le(le=2097152) |
| `ECHO_FILE_MAX_MB` | `10` | Ge(ge=1), Le(le=20) |
| `ECHO_IMAGE_MAX_PIXELS` | `8000000` | Ge(ge=10000), Le(le=25000000) |
| `ECHO_IMAGE_MAX_PAGES` | `1` | Ge(ge=1), Le(le=1) |
| `ECHO_OCR_THREADS` | `1` | Ge(ge=1), Le(le=4) |
| `ECHO_NET_CONNECTIONS` | `2` | Ge(ge=1), Le(le=4) |
| `ECHO_NET_TIMEOUT` | `10` | Ge(ge=2), Le(le=30) |
| `ECHO_NET_RESPONSE_BYTES` | `1000000` | Ge(ge=10000), Le(le=5000000) |
| `ECHO_NET_HEADER_BYTES` | `32768` | Ge(ge=4096), Le(le=65536) |
| `ECHO_NET_REDIRECTS` | `3` | Ge(ge=0), Le(le=5) |
| `ECHO_DNS_TIMEOUT` | `2` | Ge(ge=0.1), Le(le=5) |
| `ECHO_DNS_RECORD_LIMIT` | `8` | Ge(ge=1), Le(le=30) |
| `ECHO_DOMAIN_VARIANTS` | `25` | Ge(ge=1), Le(le=100) |
| `ECHO_DOMAIN_DNS_QUERIES` | `50` | Ge(ge=0), Le(le=200) |
| `ECHO_DATA_DIR` | `/data/echo` | none |
| `ECHO_DISK_QUOTA_MB` | `256` | Ge(ge=32), Le(le=16384) |
| `ECHO_USER_DISK_QUOTA_MB` | `64` | Ge(ge=10), Le(le=1024) |
| `ECHO_DISK_MIN_FREE_MB` | `64` | Ge(ge=1) |
| `ECHO_SCRATCH_QUOTA_MB` | `64` | Ge(ge=16), Le(le=256) |
| `ECHO_FOLDER_TTL_DAYS` | `30` | Ge(ge=1), Le(le=365) |
| `ECHO_MAX_FOLDERS` | `10` | Ge(ge=1), Le(le=100) |
| `ECHO_MAX_FOLDER_ITEMS` | `50` | Ge(ge=1), Le(le=500) |
| `ECHO_EXPORT_TIMEOUT` | `120` | Ge(ge=10), Le(le=600) |
| `ECHO_EXPORT_MEMORY_MB` | `768` | Ge(ge=256), Le(le=2048) |
| `ECHO_EXPORT_MAX_MB` | `40` | Ge(ge=1), Le(le=40) |
| `ECHO_BRAND_IMAGES` | `True` | none |
| `ECHO_RECENT_RESULT_MINUTES` | `30` | Ge(ge=1), Le(le=1440) |
| `ECHO_BATCH_MAX_CARDS` | `50` | Ge(ge=1), Le(le=100) |
| `ECHO_AI_ENABLED` | `False` | none |
| `ECHO_AI_INTENTS` | `False` | none |
| `ECHO_AI_ENDPOINT` | `https://api.deepseek.com` | none |
| `ECHO_AI_MODEL` | `deepseek-flash` | none |
| `ECHO_AI_KEY` | `unset (secret)` | none |
| `ECHO_AI_TIMEOUT` | `20` | Ge(ge=1), Le(le=120) |
| `ECHO_AI_MAX_INPUT_CHARS` | `6000` | Ge(ge=1000), Le(le=12000) |
| `ECHO_AI_MAX_OUTPUT_TOKENS` | `400` | Ge(ge=100), Le(le=1000) |
| `ECHO_AI_PARALLEL` | `2` | Ge(ge=1), Le(le=16) |
| `ECHO_AI_USER_DAILY_REQUESTS` | `30` | Ge(ge=0) |
| `ECHO_AI_DAILY_REQUESTS` | `300` | Ge(ge=0) |
| `ECHO_AI_DESCRIPTION_DAILY` | `200` | Ge(ge=0) |
| `ECHO_AI_TRANSLATION_DAILY` | `100` | Ge(ge=0) |
| `ECHO_AI_EXPLANATION_DAILY` | `50` | Ge(ge=0) |
| `ECHO_AI_INTENT_DAILY` | `50` | Ge(ge=0) |
| `ECHO_AI_DAILY_BUDGET_USD` | `1` | Ge(ge=0) |
| `ECHO_AI_INPUT_USD_MILLION` | `unset` | Ge(ge=0) |
| `ECHO_AI_OUTPUT_USD_MILLION` | `unset` | Ge(ge=0) |
| `ECHO_AI_PRICE_DATE` | `unset` | none |
| `ECHO_AI_RETRIES` | `1` | Ge(ge=0), Le(le=2) |
| `ECHO_AI_RETRY_MAX_DELAY` | `15` | Ge(ge=0), Le(le=60) |
| `ECHO_AI_BREAKER_FAILURES` | `4` | Ge(ge=1) |
| `ECHO_AI_BREAKER_SECONDS` | `120` | Ge(ge=10) |
| `ECHO_CATALOG_TTL_DAYS` | `30` | Ge(ge=1) |
| `ECHO_CATALOG_NEGATIVE_TTL_HOURS` | `6` | Ge(ge=1) |
| `ECHO_CATALOG_LOCK_SECONDS` | `180` | Ge(ge=30) |
| `ECHO_SOURCE_MAX_BYTES` | `200000` | Ge(ge=1000), Le(le=1000000) |

## Behaviour

- RECENT_RESULT_MINUTES limits suggestions to your own retained completed/partial username and URL checks with matching options. The user chooses whether to reuse or explicitly rerun. This never shares results between owners.
- BATCH_MAX_CARDS bounds selection size; MAX_FOLDER_ITEMS still bounds destination capacity. Batch saves are atomic and idempotent per confirmation/workspace, and preserve point-in-time personal annotations.
- Eco validates one global RUNNING job, one active job per user, one heavy slot, sequential engines and one OCR thread. Standard changes are explicit; keep all bot/worker replicas consistent. See modules.md for Compose memory budgets.
- Every new module uses its own ENABLED/TIMEOUT/MEMORY_MB plus shared file/image/network/output bounds. IMAGE_MAX_PAGES is fixed at one; PDF OCR is unsupported.
- Heavy slots use Redis leases across deployments' workers, including search, OCR, PDF and ZIP. Additional replicas do not bypass the same Redis limit. Do not share one database with different Redis namespaces.
- DATA_DIR must be a shared persistent volume for bot/worker. Disk quotas count reserved and ready files; free-space and scratch bounds also apply. Folder TTL starts at creation and is separate from history.
- ECHO_ADMINS: comma-separated numeric Telegram IDs. ECHO_BRAND changes application copy; change the Telegram bot name separately in BotFather.
- Global AI enablement and individual consent are both required. ECHO_AI_INTENTS additionally enables the bounded parser; every action is also available with buttons.
- Global active jobs means RUNNING across workers. User active jobs includes QUEUED and RUNNING. The daily job count uses retained jobs; deleting jobs can release that supplementary daily count. Global/active limits still apply.
- MAX_SITES is an upper bound per engine, not a promise of supported sites. Large coverage can hit ENGINE_TIMEOUT and produce PARTIAL results.
- MAX_DELIVERIES bounds crash recovery. LEASE_SECONDS controls recovery latency; cancellation is normally observed within five seconds.
- DOMAIN_INTERVAL applies to exact hostnames through Redis. Explicit retry jobs respect available Retry-After up to one hour, plus bounded exponential backoff and jitter.
- AI_RETRIES bounds transport retries; one additional structural repair is possible. The entire worst case is reserved atomically before paid work. Sent attempts with unknown usage retain conservative cost.
- Currency limits require reliable provider prices and usage. Zero AI_DAILY_BUDGET_USD disables external AI even when prices are unset.
- Catalogue locks automatically extend a short configured TTL to cover the bounded request duration. Cache keys include language, schema and prompt versions. Manual descriptions are protected.
- SHERLOCK_PYTHON and MAIGRET_PYTHON point to separate pinned environments. TEMP_DIR is private writable scratch space; PDF_FONT must be a readable Unicode TTF.
- Telegram exports are capped at 45 MiB including age overhead, below the regular 50 MB API limit.
- Cleanup checks run at least as frequently as the smaller of CLEANUP_INTERVAL and the 10-second worker heartbeat. PostgreSQL job TTL is enforced on access before physical cleanup.
