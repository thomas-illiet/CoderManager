"""Data migration tests for removing internal business applications."""

# ruff: noqa: SLF001

from importlib import import_module
from uuid import uuid4

import pytest
import sqlalchemy as sa

migration = import_module("migrations.versions.20260722_0012_remove_internal_applications")


def legacy_tables() -> tuple[sa.MetaData, sa.Table, sa.Table, sa.Table]:
    """Build the legacy tables required by the migration helpers."""

    metadata = sa.MetaData()
    applications = sa.Table(
        "applications",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("external_id", sa.String(255), nullable=False),
    )
    instances = sa.Table(
        "instances",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("application", sa.String(255), nullable=True),
    )
    templates = sa.Table(
        "templates",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("application", sa.String(255), nullable=True),
    )
    return metadata, applications, instances, templates


def test_migration_backfills_normalized_external_identifiers() -> None:
    """Copy uppercase external IDs to instances and scoped templates only."""

    metadata, applications, instances, templates = legacy_tables()
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    application_id = uuid4()
    instance_id = uuid4()
    scoped_template_id = uuid4()
    global_template_id = uuid4()

    with engine.begin() as connection:
        connection.execute(
            applications.insert().values(
                id=application_id,
                external_id=" external-app ",
            )
        )
        connection.execute(instances.insert().values(id=instance_id, application_id=application_id))
        connection.execute(
            templates.insert(),
            [
                {
                    "id": scoped_template_id,
                    "scope": "application",
                    "application_id": application_id,
                },
                {
                    "id": global_template_id,
                    "scope": "global",
                    "application_id": None,
                },
            ],
        )

        migration._validate_external_identifiers(connection)
        migration._backfill_external_identifiers(connection)

        assert (
            connection.scalar(
                sa.select(instances.c.application).where(instances.c.id == instance_id)
            )
            == "EXTERNAL-APP"
        )
        migrated_templates = dict(
            connection.execute(sa.select(templates.c.id, templates.c.application)).all()
        )
        assert migrated_templates[scoped_template_id] == "EXTERNAL-APP"
        assert migrated_templates[global_template_id] is None
    engine.dispose()


@pytest.mark.parametrize("external_ids", [("app", "APP"), (" app ", "APP")])
def test_migration_rejects_normalized_collisions(external_ids: tuple[str, str]) -> None:
    """Abort before schema changes when two legacy identifiers normalize identically."""

    metadata, applications, _instances, _templates = legacy_tables()
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            applications.insert(),
            [
                {"id": uuid4(), "external_id": external_ids[0]},
                {"id": uuid4(), "external_id": external_ids[1]},
            ],
        )

        with pytest.raises(RuntimeError, match="collide after normalization"):
            migration._validate_external_identifiers(connection)
    engine.dispose()


def test_application_removal_migration_has_no_downgrade() -> None:
    """Expose the intentionally irreversible migration contract."""

    with pytest.raises(RuntimeError, match="Downgrade is not supported"):
        migration.downgrade()
