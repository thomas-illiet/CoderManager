"""PostgreSQL schema selection shared by API, workers, and migrations."""

from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import AsyncEngine


class DatabaseSchemaConfigurationError(RuntimeError):
    """Raised when the configured PostgreSQL schema cannot be selected."""


def resolve_database_schema(database_url: str, database_schema: str | None) -> str | None:
    """Require a non-empty schema for PostgreSQL and ignore it for other backends."""

    if make_url(database_url).get_backend_name() != "postgresql":
        return None
    if database_schema is None or not database_schema.strip():
        msg = "CODER_MANAGER_DATABASE_SCHEMA is required for PostgreSQL connections"
        raise DatabaseSchemaConfigurationError(msg)
    return database_schema


def quote_postgresql_identifier(identifier: str) -> str:
    """Quote one PostgreSQL identifier without allowing additional search-path entries."""

    return '"' + identifier.replace('"', '""') + '"'


def validate_selected_schema(
    selected_schema: tuple[object, ...] | None,
    database_schema: str,
) -> None:
    """Require PostgreSQL to resolve the configured schema as its current schema."""

    if selected_schema is not None and selected_schema[0] == database_schema:
        return
    msg = (
        f"CODER_MANAGER_DATABASE_SCHEMA schema {database_schema!r} "
        "does not exist or is not accessible"
    )
    raise DatabaseSchemaConfigurationError(msg)


def select_database_schema(dbapi_connection: Any, database_schema: str) -> None:  # noqa: ANN401
    """Select and validate one schema on a newly opened PostgreSQL connection."""

    quoted_schema = quote_postgresql_identifier(database_schema)
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"SET SESSION search_path TO {quoted_schema}")
        cursor.execute("SELECT current_schema()")
        validate_selected_schema(cursor.fetchone(), database_schema)
        dbapi_connection.commit()
    except Exception:
        dbapi_connection.rollback()
        raise
    finally:
        cursor.close()


def configure_database_schema(
    engine: Engine | AsyncEngine,
    database_url: str,
    database_schema: str | None,
) -> str | None:
    """Configure schema selection for every new PostgreSQL DBAPI connection."""

    resolved_schema = resolve_database_schema(database_url, database_schema)
    if resolved_schema is None:
        return None

    sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine

    @event.listens_for(sync_engine, "connect")
    def set_database_schema(dbapi_connection: Any, _connection_record: Any) -> None:  # noqa: ANN401
        """Select the configured schema when the pool opens a DBAPI connection."""

        select_database_schema(dbapi_connection, resolved_schema)

    return resolved_schema
