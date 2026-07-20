"""PostgreSQL-only concurrency proof for database slot reservations."""

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from coder_manager.models import Application, Database, DatabaseAllocation
from coder_manager.models.base import Base
from coder_manager.repositories import InstanceDatabaseUnavailableError, InstanceRepository
from coder_manager.schemas import InstanceCreate

POSTGRES_URL = os.getenv("CODER_MANAGER_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    POSTGRES_URL is None,
    reason="CODER_MANAGER_TEST_POSTGRES_URL is not configured",
)


@pytest_asyncio.fixture
async def postgres_session_maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create an isolated PostgreSQL schema and remove it after the concurrency test."""

    assert POSTGRES_URL is not None
    schema = f"test_database_pool_{uuid4().hex}"
    admin_engine = create_async_engine(POSTGRES_URL)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine: AsyncEngine = create_async_engine(
        POSTGRES_URL,
        connect_args={"server_settings": {"search_path": schema}},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


async def test_concurrent_reservations_never_exceed_instance_max(
    postgres_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Serialize regional placement so two requests cannot claim the final slot."""

    application_ids = [uuid4(), uuid4()]
    async with postgres_session_maker() as session:
        session.add(
            Database(
                name="only",
                region="emea",
                instance_max=1,
                host="postgres.internal",
                port=5432,
                database_name="coder",
                username="coder",
                password_enc=b"test-only",
            )
        )
        session.add_all(
            Application(
                id=application_id,
                external_id=f"app-{index}",
                name=f"App {index}",
                whitelist=True,
            )
            for index, application_id in enumerate(application_ids)
        )
        await session.commit()

    async def reserve(application_id: UUID) -> str:
        """Provide the reserve helper used by this test scenario."""

        async with postgres_session_maker() as session:
            try:
                await InstanceRepository(session).create(
                    InstanceCreate(
                        application_id=application_id,
                        region="emea",
                        environment="development",
                    ),
                    instance_domain="code-studio",
                )
            except InstanceDatabaseUnavailableError:
                return "full"
            return "created"

    results = await asyncio.gather(*(reserve(application_id) for application_id in application_ids))

    assert sorted(results) == ["created", "full"]
    async with postgres_session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(DatabaseAllocation)) == 1
