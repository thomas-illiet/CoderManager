"""Create templates.

Revision ID: 20260719_0006
Revises: 20260719_0005
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260719_0006"
down_revision: str | None = "20260719_0005"
branch_labels: str | None = None
depends_on: str | None = None

scope_enum = sa.Enum("global", "application", name="template_scope")


def upgrade() -> None:
    """Create scoped Coder templates and their uniqueness constraints."""

    op.create_table(
        "templates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("scope", scope_enum, nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("git_url", sa.String(length=2048), nullable=False),
        sa.Column("modules", sa.JSON(), nullable=False),
        sa.Column("version", sa.String(length=255), nullable=False),
        sa.Column("min_cpu_count", sa.Integer(), nullable=False),
        sa.Column("max_cpu_count", sa.Integer(), nullable=False),
        sa.Column("min_ram_gb", sa.Integer(), nullable=False),
        sa.Column("max_ram_gb", sa.Integer(), nullable=False),
        sa.Column("min_disk_gb", sa.Integer(), nullable=False),
        sa.Column("max_disk_gb", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(name)) > 0",
            name=op.f("ck_templates_name_not_empty"),
        ),
        sa.CheckConstraint(
            "length(trim(git_url)) > 0",
            name=op.f("ck_templates_git_url_not_empty"),
        ),
        sa.CheckConstraint(
            "length(trim(version)) > 0",
            name=op.f("ck_templates_version_not_empty"),
        ),
        sa.CheckConstraint(
            "min_cpu_count > 0",
            name=op.f("ck_templates_min_cpu_count_positive"),
        ),
        sa.CheckConstraint(
            "max_cpu_count > 0",
            name=op.f("ck_templates_max_cpu_count_positive"),
        ),
        sa.CheckConstraint(
            "min_cpu_count <= max_cpu_count",
            name=op.f("ck_templates_cpu_range_valid"),
        ),
        sa.CheckConstraint(
            "min_ram_gb > 0",
            name=op.f("ck_templates_min_ram_gb_positive"),
        ),
        sa.CheckConstraint(
            "max_ram_gb > 0",
            name=op.f("ck_templates_max_ram_gb_positive"),
        ),
        sa.CheckConstraint(
            "min_ram_gb <= max_ram_gb",
            name=op.f("ck_templates_ram_range_valid"),
        ),
        sa.CheckConstraint(
            "min_disk_gb > 0",
            name=op.f("ck_templates_min_disk_gb_positive"),
        ),
        sa.CheckConstraint(
            "max_disk_gb > 0",
            name=op.f("ck_templates_max_disk_gb_positive"),
        ),
        sa.CheckConstraint(
            "min_disk_gb <= max_disk_gb",
            name=op.f("ck_templates_disk_range_valid"),
        ),
        sa.CheckConstraint(
            "(scope = 'global' AND application_id IS NULL) OR "
            "(scope = 'application' AND application_id IS NOT NULL)",
            name=op.f("ck_templates_scope_application_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_templates_application_id_applications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_templates")),
    )
    op.create_index(
        op.f("ix_templates_application_id"),
        "templates",
        ["application_id"],
        unique=False,
    )
    op.create_index(
        "uq_templates_global_name_ci",
        "templates",
        [sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("scope = 'global'"),
        sqlite_where=sa.text("scope = 'global'"),
    )
    op.create_index(
        "uq_templates_application_name_ci",
        "templates",
        ["application_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("scope = 'application'"),
        sqlite_where=sa.text("scope = 'application'"),
    )


def downgrade() -> None:
    """Drop templates and their PostgreSQL enum type."""

    op.drop_index("uq_templates_application_name_ci", table_name="templates")
    op.drop_index("uq_templates_global_name_ci", table_name="templates")
    op.drop_index(op.f("ix_templates_application_id"), table_name="templates")
    op.drop_table("templates")
    scope_enum.drop(op.get_bind(), checkfirst=True)
