"""Shared API and database fixtures."""

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from coder_manager.config import Settings, get_settings
from coder_manager.database import get_session
from coder_manager.main import app
from coder_manager.models import Database
from coder_manager.models.base import Base
from coder_manager.tasks import (
    retry_job_executions,
    step_01_create_schema,
    step_01_create_workspace,
    step_01_delete_workspace,
    step_01_remove_workspaces,
    step_01_sync_database,
    step_01_sync_template,
    step_01_update_instance,
    step_01_update_workspace,
    step_02_create_instance,
    step_02_remove_instance,
    step_03_bootstrap_admin,
    step_03_remove_schema,
    step_04_remove_local_configuration,
    step_04_sync_templates,
)
from coder_manager.worker_database import derive_sync_database_url

TEST_CRYPTO_KEY = "MDAxMTIyMzM0NDU1NjY3Nzg4ODlhYWJiY2NkZGVlZmY="


@pytest.fixture(autouse=True)
def disable_celery_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep API tests independent from a running Redis broker."""

    for task in (
        retry_job_executions,
        step_01_create_schema,
        step_02_create_instance,
        step_03_bootstrap_admin,
        step_01_update_instance,
        step_01_remove_workspaces,
        step_02_remove_instance,
        step_03_remove_schema,
        step_04_remove_local_configuration,
        step_04_sync_templates,
        step_01_create_workspace,
        step_01_update_workspace,
        step_01_delete_workspace,
        step_01_sync_database,
        step_01_sync_template,
    ):
        monkeypatch.setattr(task, "delay", MagicMock())


@pytest_asyncio.fixture
async def session_maker(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Return an isolated file-backed database session factory."""

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    engine = create_async_engine(
        database_url,
        connect_args={"check_same_thread": False},
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with maker() as session:
        session.add(
            Database(
                id=uuid4(),
                name="test",
                instance_max=100,
                host="postgres.internal",
                port=5432,
                database_name="coder",
                username="coder_manager",
                password_enc=b"test-only",
            )
        )
        await session.commit()
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def sync_session_maker(
    session_maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[sessionmaker[Session]]:
    """Return a sync worker session factory sharing the async fixture database."""

    async_engine = session_maker.kw["bind"]
    engine = create_engine(
        derive_sync_database_url(str(async_engine.url)),
        connect_args={"check_same_thread": False},
        pool_size=1,
        max_overflow=0,
    )
    maker = sessionmaker(engine, class_=Session, expire_on_commit=False)
    yield maker
    engine.dispose()


@pytest_asyncio.fixture
async def client(
    session_maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """Return an API client backed by an isolated in-memory database."""

    async def override_session() -> AsyncIterator[AsyncSession]:
        """Provide the override session helper used by this test scenario."""

        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(crypto_key=TEST_CRYPTO_KEY)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api_client:
        yield api_client

    app.dependency_overrides.clear()
