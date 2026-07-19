"""Add the application whitelist flag.

Revision ID: 20260719_0004
Revises: 20260719_0003
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260719_0004"
down_revision: str | None = "20260719_0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add a disabled-by-default whitelist flag to applications."""

    op.add_column(
        "applications",
        sa.Column("whitelist", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    """Remove the application whitelist flag."""

    op.drop_column("applications", "whitelist")
