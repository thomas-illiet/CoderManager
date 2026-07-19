"""Create the instances table.

Revision ID: 20260719_0003
Revises: 20260719_0002
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260719_0003"
down_revision: str | None = "20260719_0002"
branch_labels: str | None = None
depends_on: str | None = None

region_enum = sa.Enum("emea", "apac", "amer", name="instance_region")
environment_enum = sa.Enum(
    "development",
    "staging",
    "production",
    name="instance_environment",
)
status_enum = sa.Enum("pending", "running", "success", "error", name="instance_status")


def upgrade() -> None:
    """Create instances and its relational constraints."""

    op.create_table(
        "instances",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("region", region_enum, nullable=False),
        sa.Column("environment", environment_enum, nullable=False),
        sa.Column(
            "action",
            sa.String(length=255),
            server_default="creating",
            nullable=False,
        ),
        sa.Column(
            "status",
            status_enum,
            server_default="pending",
            nullable=False,
        ),
        sa.Column("instance_url", sa.String(length=2048), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(action)) > 0",
            name=op.f("ck_instances_action_not_empty"),
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_instances_application_id_applications"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instances")),
        sa.UniqueConstraint(
            "application_id",
            "region",
            "environment",
            name=op.f("uq_instances_application_region_environment"),
        ),
        sa.UniqueConstraint(
            "instance_url",
            name=op.f("uq_instances_instance_url"),
        ),
    )
    op.create_index(
        op.f("ix_instances_application_id"),
        "instances",
        ["application_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop instances and its PostgreSQL enum types."""

    op.drop_index(op.f("ix_instances_application_id"), table_name="instances")
    op.drop_table("instances")
    status_enum.drop(op.get_bind(), checkfirst=True)
    environment_enum.drop(op.get_bind(), checkfirst=True)
    region_enum.drop(op.get_bind(), checkfirst=True)
