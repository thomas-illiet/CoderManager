"""Synchronous Celery worker database tests."""

# ruff: noqa: SLF001

from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.pool import QueuePool

from coder_manager import worker_database
from coder_manager.celery_app import (
    initialize_worker_process_database,
    shutdown_worker_process_database,
)


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


def test_worker_process_database_lifecycle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify the worker process database lifecycle scenario."""

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}"
    monkeypatch.setattr(
        worker_database,
        "get_settings",
        lambda: SimpleNamespace(database_url=database_url),
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
