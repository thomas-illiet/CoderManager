"""Database schema configuration tests."""

# ruff: noqa: SLF001

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coder_manager import worker_database
from coder_manager.database_schema import (
    DatabaseSchemaConfigurationError,
    quote_postgresql_identifier,
    resolve_database_schema,
    select_database_schema,
)

PROJECT_ROOT = Path(__file__).parents[1]


class FakeCursor:
    """Record DBAPI statements and return one configured current schema."""

    def __init__(self, selected_schema: tuple[str] | None) -> None:
        """Initialize the cursor with the schema returned by current_schema()."""

        self.selected_schema = selected_schema
        self.statements: list[str] = []
        self.closed = False

    def execute(self, statement: str) -> None:
        """Record one statement."""

        self.statements.append(statement)

    def fetchone(self) -> tuple[str] | None:
        """Return the configured current schema."""

        return self.selected_schema

    def close(self) -> None:
        """Record cursor closure."""

        self.closed = True


class FakeConnection:
    """Minimal DBAPI connection for schema-selection tests."""

    def __init__(self, selected_schema: tuple[str] | None) -> None:
        """Initialize transaction counters and the recording cursor."""

        self.database_cursor = FakeCursor(selected_schema)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        """Return the recording cursor."""

        return self.database_cursor

    def commit(self) -> None:
        """Record a commit."""

        self.commits += 1

    def rollback(self) -> None:
        """Record a rollback."""

        self.rollbacks += 1


@pytest.mark.parametrize("database_schema", [None, "", "   "])
def test_postgresql_database_schema_is_required(database_schema: str | None) -> None:
    """Reject missing or empty PostgreSQL schema configuration."""

    with pytest.raises(
        DatabaseSchemaConfigurationError,
        match="CODER_MANAGER_DATABASE_SCHEMA is required",
    ):
        resolve_database_schema(
            "postgresql+asyncpg://user:secret@postgres/database",
            database_schema,
        )


def test_non_postgresql_backend_does_not_require_schema() -> None:
    """Keep SQLite test engines independent from PostgreSQL schema selection."""

    assert resolve_database_schema("sqlite+aiosqlite:////tmp/test.db", None) is None


def test_database_schema_is_safely_selected() -> None:
    """Quote the schema as one identifier and commit the session-level selection."""

    database_schema = 'coder manager"; RESET search_path; --'
    connection = FakeConnection((database_schema,))

    select_database_schema(connection, database_schema)

    assert quote_postgresql_identifier(database_schema) == (
        '"coder manager""; RESET search_path; --"'
    )
    assert connection.database_cursor.statements == [
        'SET SESSION search_path TO "coder manager""; RESET search_path; --"',
        "SELECT current_schema()",
    ]
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.database_cursor.closed is True


def test_inaccessible_database_schema_is_rejected() -> None:
    """Reject a schema PostgreSQL cannot resolve as current."""

    connection = FakeConnection(None)

    with pytest.raises(
        DatabaseSchemaConfigurationError,
        match="does not exist or is not accessible",
    ):
        select_database_schema(connection, "missing")

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.database_cursor.closed is True


def test_worker_database_requires_postgresql_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail worker database initialization when its PostgreSQL schema is absent."""

    monkeypatch.setattr(
        worker_database,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+asyncpg://user:secret@postgres/database",
            database_schema=None,
        ),
    )
    worker_database.shutdown_worker_database()

    with pytest.raises(
        DatabaseSchemaConfigurationError,
        match="CODER_MANAGER_DATABASE_SCHEMA is required",
    ):
        worker_database.initialize_worker_database()

    assert worker_database._worker_engine is None
    assert worker_database._worker_session_maker is None


@pytest.mark.parametrize(
    "command",
    [
        [sys.executable, "-c", "import coder_manager.database"],
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(PROJECT_ROOT / "alembic.ini"),
            "upgrade",
            "head",
            "--sql",
        ],
    ],
    ids=["api", "alembic"],
)
def test_database_consumers_reject_empty_schema(
    command: list[str],
) -> None:
    """Fail API and Alembic initialization before opening a PostgreSQL connection."""

    environment = os.environ.copy()
    environment["CODER_MANAGER_DATABASE_SCHEMA"] = ""

    result = subprocess.run(  # noqa: S603
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "CODER_MANAGER_DATABASE_SCHEMA is required" in result.stderr
