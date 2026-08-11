"""PostgreSQL-only concurrency proof for database slot reservations."""

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from coder_manager.database_schema import configure_database_schema
from coder_manager.models import Database, DatabaseAllocation
from coder_manager.models.base import Base
from coder_manager.repositories import InstanceDatabaseUnavailableError, InstanceRepository
from coder_manager.schemas import InstanceCreate
from coder_manager.worker_database import derive_sync_database_url

POSTGRES_URL = os.getenv("CODER_MANAGER_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    POSTGRES_URL is None,
    reason="CODER_MANAGER_TEST_POSTGRES_URL is not configured",
)


async def test_async_and_sync_engines_select_configured_schema() -> None:
    """Use the same isolated PostgreSQL schema from async and sync engines."""

    assert POSTGRES_URL is not None
    schema = f"test_database_schema_{uuid4().hex}"
    admin_engine = create_async_engine(POSTGRES_URL)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    async_engine = create_async_engine(POSTGRES_URL)
    configure_database_schema(async_engine, POSTGRES_URL, schema)
    sync_engine = create_engine(derive_sync_database_url(POSTGRES_URL))
    configure_database_schema(sync_engine, POSTGRES_URL, schema)
    try:
        async with async_engine.begin() as connection:
            assert await connection.scalar(text("SELECT current_schema()")) == schema
            await connection.execute(text("CREATE TABLE schema_probe (id integer)"))

        def read_sync_schema() -> tuple[str | None, str | None]:
            """Read the current schema and probe table through the sync engine."""

            with sync_engine.connect() as connection:
                selected = connection.scalar(text("SELECT current_schema()"))
                probe = connection.scalar(text("SELECT to_regclass('schema_probe')::text"))
                return selected, probe

        assert await asyncio.to_thread(read_sync_schema) == (schema, "schema_probe")

        async with admin_engine.connect() as connection:
            public_probe = await connection.scalar(
                text("SELECT to_regclass('public.schema_probe')")
            )
            assert public_probe is None
    finally:
        await async_engine.dispose()
        sync_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


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
    """Serialize global placement so two requests cannot claim the final slot."""

    applications = ["APP-1", "APP-2"]
    async with postgres_session_maker() as session:
        session.add(
            Database(
                name="only",
                instance_max=1,
                host="postgres.internal",
                port=5432,
                database_name="coder",
                username="coder",
                password_enc=b"test-only",
            )
        )
        await session.commit()

    async def reserve(application: str) -> str:
        """Provide the reserve helper used by this test scenario."""

        async with postgres_session_maker() as session:
            try:
                await InstanceRepository(session).create(
                    InstanceCreate(
                        application=application,
                        environment="development",
                    ),
                    instance_domain="code-studio",
                )
            except InstanceDatabaseUnavailableError:
                return "full"
            return "created"

    results = await asyncio.gather(*(reserve(application) for application in applications))

    assert sorted(results) == ["created", "full"]
    async with postgres_session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(DatabaseAllocation)) == 1
