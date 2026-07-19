"""Synchronous SQLAlchemy engine lifecycle for Celery worker processes."""

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from coder_manager.config import get_settings

_SYNC_DRIVERS = {
    "postgresql+asyncpg": "postgresql+psycopg",
    "sqlite+aiosqlite": "sqlite+pysqlite",
}

_worker_engine: Engine | None = None
_worker_session_maker: sessionmaker[Session] | None = None


def derive_sync_database_url(database_url: str) -> str:
    """Replace a supported async SQLAlchemy driver with its synchronous counterpart."""

    url = make_url(database_url)
    drivername = _SYNC_DRIVERS.get(url.drivername, url.drivername)
    return url.set(drivername=drivername).render_as_string(hide_password=False)


def initialize_worker_database() -> None:
    """Create one synchronous engine and session factory in the current worker process."""

    global _worker_engine, _worker_session_maker  # noqa: PLW0603

    if _worker_engine is not None:
        return
    _worker_engine = create_engine(
        derive_sync_database_url(get_settings().database_url),
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
    )
    _worker_session_maker = sessionmaker(_worker_engine, expire_on_commit=False)


def get_worker_session_maker() -> sessionmaker[Session]:
    """Return the process-local synchronous worker session factory."""

    if _worker_session_maker is None:
        initialize_worker_database()
    if _worker_session_maker is None:  # pragma: no cover - defensive invariant
        msg = "worker database initialization did not create a session factory"
        raise RuntimeError(msg)
    return _worker_session_maker


def shutdown_worker_database() -> None:
    """Dispose the process-local worker pool when Celery shuts the process down."""

    global _worker_engine, _worker_session_maker  # noqa: PLW0603

    if _worker_engine is not None:
        _worker_engine.dispose()
    _worker_engine = None
    _worker_session_maker = None
