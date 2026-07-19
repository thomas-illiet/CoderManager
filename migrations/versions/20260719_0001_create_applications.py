"""Create the applications table.

Revision ID: 20260719_0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260719_0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create applications and its external identifier index."""

    op.create_table(
        "applications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_applications")),
    )
    op.create_index(
        op.f("ix_applications_external_id"),
        "applications",
        ["external_id"],
        unique=True,
    )


def downgrade() -> None:
    """Drop applications."""

    op.drop_index(op.f("ix_applications_external_id"), table_name="applications")
    op.drop_table("applications")
