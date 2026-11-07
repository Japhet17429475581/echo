import os
import uuid
from pathlib import Path

import fakeredis.aioredis
import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from echo.config import Settings
from echo.domain import Observation, Status
from echo.services import Jobs
from echo.storage import Base, Gate


@pytest.fixture
def config(tmp_path):
    font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if not font.exists():
        font = Path("C:/Windows/Fonts/arial.ttf")
    return Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite://",
        temp_dir=tmp_path,
        data_dir=tmp_path / "vault",
        resource_profile="standard",
        pdf_font=font,
        user_active_jobs=10,
        ai_retry_max_delay=0,
        ai_breaker_failures=10,
    )


@pytest.fixture
async def sessions(tmp_path):
    url = os.getenv("ECHO_TEST_DATABASE_URL")
    schema, admin = None, None
    if url:
        schema = "echo_test_" + uuid.uuid4().hex
        admin = create_async_engine(url, hide_parameters=True)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            url, hide_parameters=True, connect_args={"server_settings": {"search_path": schema}}
        )
    else:
        engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "test.sqlite"))

        @event.listens_for(engine.sync_engine, "connect")
        def foreign_keys(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        session.add(Gate(id=1))
    yield factory
    await engine.dispose()
    if admin:
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def jobs(sessions, config):
    return Jobs(sessions, config)


@pytest.fixture
def observation():
    return Observation(
        engine="sherlock",
        version="0.16.2",
        site_key="GitHub",
        site_name="GitHub",
        domain="github.com",
        username="echo_fixture_user",
        url="https://github.com/echo_fixture_user",
        raw_status="Claimed",
        status=Status.POSSIBLE_MATCH,
        http_code=200,
        check_method="status_code",
    )
