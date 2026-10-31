import asyncio

from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401
from alembic import context
from app.config import settings
from app.database import Base

target_metadata = Base.metadata


def run_migrations_online():
    engine = create_async_engine(settings.database_url)

    async def run():
        async with engine.connect() as conn:
            await conn.run_sync(lambda c: (context.configure(connection=c, target_metadata=target_metadata, compare_type=True),
                                           context.run_migrations()))
        await engine.dispose()
    asyncio.run(run())


if context.is_offline_mode():
    context.configure(url=settings.database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
