"""Add the application creation timestamp.

Revision ID: 20260719_0002
Revises: 20260719_0001
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260719_0002"
down_revision: str | None = "20260719_0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add a database-generated creation timestamp to applications."""

    op.add_column(
        "applications",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Remove the application creation timestamp."""

    op.drop_column("applications", "created_at")
