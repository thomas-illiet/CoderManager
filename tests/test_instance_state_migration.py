"""Strict instance-state migration contract tests."""

from importlib import import_module
from typing import Any

import pytest

migration = import_module("migrations.versions.d3739fe0ca6c_add_strict_instance_state")


class MigrationBind:
    """Return a controlled instance count to the migration."""

    def __init__(self, instance_count: int) -> None:
        """Store the row count returned by the fake connection."""

        self.instance_count = instance_count

    def scalar(self, statement: object) -> int:
        """Validate the guard query and return the configured count."""

        assert str(statement) == "SELECT count(*) FROM instances"
        return self.instance_count


def test_instance_state_migration_refuses_nonempty_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Abort before any DDL when an historical instance row exists."""

    monkeypatch.setattr(migration.op, "get_bind", lambda: MigrationBind(1))
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda *_args, **_kwargs: pytest.fail("DDL must not run"),
    )

    with pytest.raises(RuntimeError, match="instances must be empty"):
        migration.upgrade()


def test_instance_state_migration_adds_strict_column_without_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create the enum before adding a non-null column with no SQL default."""

    bind = MigrationBind(0)
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        migration.sa.Enum,
        "create",
        lambda _enum, observed_bind, *, checkfirst: calls.append(
            ("create_enum", (observed_bind, checkfirst))
        ),
    )
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: calls.append(("add_column", (table, column))),
    )
    monkeypatch.setattr(
        migration.op,
        "alter_column",
        lambda table, column, **kwargs: calls.append(("alter_column", (table, column, kwargs))),
    )

    migration.upgrade()

    assert [name for name, _payload in calls] == [
        "create_enum",
        "add_column",
        "alter_column",
    ]
    table, column = calls[1][1]
    assert table == "instances"
    assert column.name == "state"
    assert column.nullable is False
    assert column.server_default is None
    altered_table, altered_column, altered_options = calls[2][1]
    assert altered_table == "instances"
    assert altered_column == "slug"
    assert altered_options["nullable"] is False
    assert altered_options["existing_type"].length == 12
