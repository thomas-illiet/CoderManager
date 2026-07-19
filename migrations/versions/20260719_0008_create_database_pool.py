"""Create the managed database pool and instance allocations.

Revision ID: 20260719_0008
Revises: 20260719_0007
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260719_0008"
down_revision: str | None = "20260719_0007"
branch_labels: str | None = None
depends_on: str | None = None

region_enum = postgresql.ENUM(
    "emea",
    "apac",
    "amer",
    name="instance_region",
    create_type=False,
)


def upgrade() -> None:
    """Create database pool entries and one allocation per instance."""

    op.create_table(
        "databases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("region", region_enum, nullable=False),
        sa.Column("instance_max", sa.Integer(), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), server_default="5432", nullable=False),
        sa.Column("database_name", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_enc", sa.LargeBinary(), nullable=False),
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
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_databases_name_not_empty")),
        sa.CheckConstraint(
            "instance_max > 0",
            name=op.f("ck_databases_instance_max_positive"),
        ),
        sa.CheckConstraint("length(trim(host)) > 0", name=op.f("ck_databases_host_not_empty")),
        sa.CheckConstraint(
            "port >= 1 AND port <= 65535",
            name=op.f("ck_databases_port_valid"),
        ),
        sa.CheckConstraint(
            "length(trim(database_name)) > 0",
            name=op.f("ck_databases_database_name_not_empty"),
        ),
        sa.CheckConstraint(
            "length(trim(username)) > 0",
            name=op.f("ck_databases_username_not_empty"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_databases")),
    )
    op.create_index(op.f("ix_databases_region"), "databases", ["region"], unique=False)
    op.create_index(
        "uq_databases_name_ci",
        "databases",
        [sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "database_allocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("database_id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("schema_name", sa.String(length=63), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(schema_name)) > 0",
            name=op.f("ck_database_allocations_schema_name_not_empty"),
        ),
        sa.ForeignKeyConstraint(
            ["database_id"],
            ["databases.id"],
            name=op.f("fk_database_allocations_database_id_databases"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["instances.id"],
            name=op.f("fk_database_allocations_instance_id_instances"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_database_allocations")),
        sa.UniqueConstraint(
            "instance_id",
            name=op.f("uq_database_allocations_instance_id"),
        ),
        sa.UniqueConstraint(
            "database_id",
            "schema_name",
            name=op.f("uq_database_allocations_database_schema"),
        ),
    )
    op.create_index(
        op.f("ix_database_allocations_database_id"),
        "database_allocations",
        ["database_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop allocations before their managed database parents."""

    op.drop_index(
        op.f("ix_database_allocations_database_id"),
        table_name="database_allocations",
    )
    op.drop_table("database_allocations")
    op.drop_index("uq_databases_name_ci", table_name="databases")
    op.drop_index(op.f("ix_databases_region"), table_name="databases")
    op.drop_table("databases")
