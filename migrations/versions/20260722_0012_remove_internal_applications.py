"""Replace internal applications with normalized external identifiers.

Revision ID: 20260722_0012
Revises: 20260720_0011
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260722_0012"
down_revision: str | None = "20260720_0011"
branch_labels: str | None = None
depends_on: str | None = None


def _validate_external_identifiers(connection: sa.Connection) -> None:
    """Abort before schema changes when uppercasing would merge applications."""

    invalid_identifier = connection.execute(
        sa.text("SELECT external_id FROM applications WHERE length(trim(external_id)) = 0 LIMIT 1")
    ).first()
    if invalid_identifier is not None:
        msg = "Cannot remove applications: an external_id is empty after trimming"
        raise RuntimeError(msg)

    collision = connection.execute(
        sa.text(
            "SELECT upper(trim(external_id)) AS normalized_external_id "
            "FROM applications GROUP BY upper(trim(external_id)) "
            "HAVING count(*) > 1 LIMIT 1"
        )
    ).scalar_one_or_none()
    if collision is not None:
        msg = (
            "Cannot remove applications: external_id values collide after normalization: "
            f"{collision}"
        )
        raise RuntimeError(msg)


def _backfill_external_identifiers(connection: sa.Connection) -> None:
    """Copy normalized external identifiers to instances and scoped templates."""

    applications = sa.table(
        "applications",
        sa.column("id", sa.Uuid()),
        sa.column("external_id", sa.String()),
    )
    instances = sa.table(
        "instances",
        sa.column("application_id", sa.Uuid()),
        sa.column("application", sa.String()),
    )
    templates = sa.table(
        "templates",
        sa.column("application_id", sa.Uuid()),
        sa.column("application", sa.String()),
    )
    normalized_external_id = (
        sa.select(sa.func.upper(sa.func.trim(applications.c.external_id)))
        .where(applications.c.id == instances.c.application_id)
        .scalar_subquery()
    )
    connection.execute(instances.update().values(application=normalized_external_id))
    normalized_template_external_id = (
        sa.select(sa.func.upper(sa.func.trim(applications.c.external_id)))
        .where(applications.c.id == templates.c.application_id)
        .scalar_subquery()
    )
    connection.execute(
        templates.update()
        .where(templates.c.application_id.is_not(None))
        .values(application=normalized_template_external_id)
    )


def upgrade() -> None:
    """Replace application foreign keys and remove the internal applications table."""

    connection = op.get_bind()
    _validate_external_identifiers(connection)

    op.add_column("instances", sa.Column("application", sa.String(length=255), nullable=True))
    op.add_column("templates", sa.Column("application", sa.String(length=255), nullable=True))
    _backfill_external_identifiers(connection)

    missing_instance = connection.execute(
        sa.text("SELECT id FROM instances WHERE application IS NULL LIMIT 1")
    ).first()
    missing_template = connection.execute(
        sa.text(
            "SELECT id FROM templates WHERE scope = 'application' AND application IS NULL LIMIT 1"
        )
    ).first()
    if missing_instance is not None or missing_template is not None:
        msg = "Cannot remove applications: a referenced external_id could not be migrated"
        raise RuntimeError(msg)

    op.drop_index(op.f("ix_instances_application_id"), table_name="instances")
    op.drop_constraint(
        op.f("uq_instances_application_region_environment"),
        "instances",
        type_="unique",
    )
    op.drop_constraint(
        op.f("fk_instances_application_id_applications"),
        "instances",
        type_="foreignkey",
    )
    op.drop_column("instances", "application_id")
    op.alter_column("instances", "application", existing_type=sa.String(255), nullable=False)
    op.create_check_constraint(
        op.f("ck_instances_application_not_empty"),
        "instances",
        "length(trim(application)) > 0",
    )
    op.create_check_constraint(
        op.f("ck_instances_application_normalized"),
        "instances",
        "application = upper(trim(application))",
    )
    op.create_unique_constraint(
        op.f("uq_instances_application_region_environment"),
        "instances",
        ["application", "region", "environment"],
    )
    op.create_index(
        op.f("ix_instances_application"),
        "instances",
        ["application"],
        unique=False,
    )

    op.drop_index("uq_templates_application_name_ci", table_name="templates")
    op.drop_index(op.f("ix_templates_application_id"), table_name="templates")
    op.drop_constraint(
        op.f("ck_templates_scope_application_consistent"),
        "templates",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_templates_application_id_applications"),
        "templates",
        type_="foreignkey",
    )
    op.drop_column("templates", "application_id")
    op.create_check_constraint(
        op.f("ck_templates_scope_application_consistent"),
        "templates",
        "(scope = 'global' AND application IS NULL) OR "
        "(scope = 'application' AND application IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_templates_application_normalized"),
        "templates",
        "application IS NULL OR (length(trim(application)) > 0 "
        "AND application = upper(trim(application)))",
    )
    op.create_index(
        op.f("ix_templates_application"),
        "templates",
        ["application"],
        unique=False,
    )
    op.create_index(
        "uq_templates_application_name_ci",
        "templates",
        ["application", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("scope = 'application'"),
        sqlite_where=sa.text("scope = 'application'"),
    )

    op.drop_table("applications")


def downgrade() -> None:
    """Reject downgrade because removed application metadata cannot be reconstructed."""

    msg = "Downgrade is not supported after removing internal applications"
    raise RuntimeError(msg)
