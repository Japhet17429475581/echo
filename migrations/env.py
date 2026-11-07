import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from echo.config import settings
from echo.storage import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings().database_url.replace("%", "%%"))


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = create_async_engine(settings().database_url, hide_parameters=True)
    async with engine.connect() as conn:
        await conn.run_sync(migrate)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=settings().database_url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
