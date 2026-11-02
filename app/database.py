from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

_kw: dict = {"pool_pre_ping": True}
if not settings.database_url.startswith("sqlite"):
    _kw.update(pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow, pool_timeout=10, pool_recycle=1800)
    if "asyncpg" in settings.database_url:
        _kw["connect_args"] = {"timeout": 10, "server_settings": {"statement_timeout": str(settings.db_statement_timeout_ms)}}
engine = create_async_engine(settings.database_url, **_kw)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
