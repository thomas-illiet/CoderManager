"""Synchronous Celery worker database tests."""

# ruff: noqa: SLF001

from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL
from sqlalchemy.pool import QueuePool

from coder_manager import worker_database
from coder_manager.celery_app import (
    initialize_worker_process_database,
    shutdown_worker_process_database,
)
from coder_manager.config import Settings, get_settings


def test_derive_sync_database_url() -> None:
    """Verify the derive sync database url scenario."""

    assert (
        worker_database.derive_sync_database_url(
            "postgresql+asyncpg://user:secret@postgres:5432/database"
        )
        == "postgresql+psycopg://user:secret@postgres:5432/database"
    )
    assert (
        worker_database.derive_sync_database_url("sqlite+aiosqlite:////tmp/database.db")
        == "sqlite+pysqlite:////tmp/database.db"
    )


@pytest.mark.parametrize("database_schema", [None, "", "   "])
def test_database_schema_is_required(database_schema: str | None) -> None:
    """Reject an absent or empty schema when a database consumer requests it."""

    with pytest.raises(ValueError, match="CODER_MANAGER_DATABASE_SCHEMA is required"):
        Settings(database_schema=database_schema).require_database_schema()


def test_alembic_accepts_url_encoded_database_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep URL-encoded percent signs intact across Alembic configuration interpolation."""

    project_root = Path(__file__).parents[1]
    database_url = URL.create(
        "postgresql+asyncpg",
        username="user",
        password="*%?",  # noqa: S106 - regression-only credential
        host="postgres",
        port=5432,
        database="database",
    ).render_as_string(hide_password=False)
    monkeypatch.setenv("CODER_MANAGER_DATABASE_URL", database_url)
    monkeypatch.setenv("CODER_MANAGER_DATABASE_SCHEMA", "public")
    get_settings.cache_clear()

    output = StringIO()
    alembic_config = Config(project_root / "alembic.ini", output_buffer=output)
    try:
        command.upgrade(alembic_config, "head", sql=True)
    finally:
        get_settings.cache_clear()
    assert alembic_config.get_main_option("sqlalchemy.url") == database_url
    assert "CREATE TABLE alembic_version" in output.getvalue()


def test_worker_database_passes_schema_to_psycopg(monkeypatch) -> None:
    """Pass the configured PostgreSQL search path directly to psycopg."""

    settings = Settings(
        database_url="postgresql+asyncpg://user:secret@postgres:5432/database",
        database_schema="coder_manager_schema",
    )
    engine = MagicMock()
    create_engine = MagicMock(return_value=engine)
    worker_database.shutdown_worker_database()
    monkeypatch.setattr(worker_database, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_database, "create_engine", create_engine)

    worker_database.initialize_worker_database()

    create_engine.assert_called_once_with(
        "postgresql+psycopg://user:secret@postgres:5432/database",
        connect_args={"options": "-csearch_path=coder_manager_schema"},
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
    )
    worker_database.shutdown_worker_database()
    engine.dispose.assert_called_once_with()


def test_worker_process_database_lifecycle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify the worker process database lifecycle scenario."""

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}"
    monkeypatch.setattr(
        worker_database,
        "get_settings",
        lambda: SimpleNamespace(database_url=database_url, database_schema=None),
    )
    worker_database.shutdown_worker_database()

    initialize_worker_process_database()
    first_engine = worker_database._worker_engine
    first_maker = worker_database.get_worker_session_maker()
    initialize_worker_process_database()

    assert first_engine is not None
    assert isinstance(first_engine.pool, QueuePool)
    assert worker_database._worker_engine is first_engine
    assert worker_database.get_worker_session_maker() is first_maker
    assert first_engine.pool.size() == 1

    shutdown_worker_process_database()
    assert worker_database._worker_engine is None
    assert worker_database._worker_session_maker is None
