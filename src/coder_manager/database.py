"""Database engine, metadata, and session dependency."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from coder_manager.config import get_settings
from coder_manager.database_schema import configure_database_schema

settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
configure_database_schema(engine, settings.database_url, settings.database_schema)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield one database session per request."""

    async with async_session_maker() as session:
        yield session
