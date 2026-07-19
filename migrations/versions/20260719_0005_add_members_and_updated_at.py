"""Add instance members and update timestamps.

Revision ID: 20260719_0005
Revises: 20260719_0004
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260719_0005"
down_revision: str | None = "20260719_0004"
branch_labels: str | None = None
depends_on: str | None = None

role_enum = sa.Enum("user", "admin", name="member_role")
status_enum = sa.Enum("pending", "running", "success", "error", name="member_status")


def upgrade() -> None:
    """Add instance update tracking and instance-scoped members."""

    op.add_column(
        "instances",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_table(
        "members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("role", role_enum, nullable=False),
        sa.Column("action", sa.String(length=255), server_default="creating", nullable=False),
        sa.Column("status", status_enum, server_default="pending", nullable=False),
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
            "length(trim(username)) > 0",
            name=op.f("ck_members_username_not_empty"),
        ),
        sa.CheckConstraint(
            "username = lower(trim(username))",
            name=op.f("ck_members_username_normalized"),
        ),
        sa.CheckConstraint(
            "length(trim(action)) > 0",
            name=op.f("ck_members_action_not_empty"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["instances.id"],
            name=op.f("fk_members_instance_id_instances"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_members")),
        sa.UniqueConstraint(
            "instance_id",
            "username",
            name=op.f("uq_members_instance_username"),
        ),
    )
    op.create_index(
        op.f("ix_members_instance_id"),
        "members",
        ["instance_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove instance members and update timestamps."""

    op.drop_index(op.f("ix_members_instance_id"), table_name="members")
    op.drop_table("members")
    status_enum.drop(op.get_bind(), checkfirst=True)
    role_enum.drop(op.get_bind(), checkfirst=True)
    op.drop_column("instances", "updated_at")
