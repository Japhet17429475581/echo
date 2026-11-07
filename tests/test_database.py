import pytest
from sqlalchemy import event

from echo.config import Settings
from echo.database import database_engine


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+asyncpg"])
async def test_provider_dsn_keeps_ssl_and_encoded_credentials(scheme):
    value = (
        f"{scheme}://fixture:p%40ss%3Aword@db.example.test:5432/echo?sslmode=require&application_name=echo"
    )
    config = Settings(_env_file=None, database_url=value)
    engine = database_engine(config.database_url)

    class CapturedConnection(Exception):
        pass

    @event.listens_for(engine.sync_engine, "do_connect")
    def inspect_driver(dialect, record, args, kwargs):
        assert kwargs["dsn"] == value.replace(scheme + "://", "postgresql://", 1)
        assert "sslmode" not in kwargs
        assert "p%40ss%3Aword" in kwargs["dsn"]
        raise CapturedConnection()

    try:
        with pytest.raises(CapturedConnection):
            async with engine.connect():
                pass
    finally:
        await engine.dispose()


async def test_sqlite_development_still_works():
    engine = database_engine("sqlite+aiosqlite://")
    try:
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql("SELECT 1")).scalar() == 1
    finally:
        await engine.dispose()
