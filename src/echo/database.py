"""Provider connection strings are parsed by asyncpg, including native SSL options."""

from sqlalchemy.ext.asyncio import create_async_engine


def normalize_database_url(value: str) -> str:
    value = value.strip()
    scheme, separator, address = value.partition("://")
    if separator and scheme.lower() in {"postgres", "postgresql"}:
        return "postgresql+asyncpg://" + address
    return value


def database_engine(value: str):
    value = normalize_database_url(value)
    if value.startswith("postgresql+asyncpg://"):
        # Passing a native DSN preserves sslmode/sslrootcert/application_name rather
        # than forwarding libpq query keys as unsupported asyncpg.connect kwargs.
        dsn = "postgresql://" + value.partition("://")[2]
        return create_async_engine(
            "postgresql+asyncpg://", connect_args={"dsn": dsn}, pool_pre_ping=True, hide_parameters=True
        )
    return create_async_engine(value, pool_pre_ping=True, hide_parameters=True)
