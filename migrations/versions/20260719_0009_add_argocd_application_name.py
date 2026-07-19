"""Persist the Argo CD Application attached to an instance.

Revision ID: 20260719_0009
Revises: 20260719_0008
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260719_0009"
down_revision: str | None = "20260719_0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the nullable unique Argo CD Application name."""

    op.add_column(
        "instances",
        sa.Column("argocd_application_name", sa.String(length=63), nullable=True),
    )
    op.create_unique_constraint(
        op.f("uq_instances_argocd_application_name"),
        "instances",
        ["argocd_application_name"],
    )


def downgrade() -> None:
    """Remove the Argo CD Application attachment."""

    op.drop_constraint(
        op.f("uq_instances_argocd_application_name"),
        "instances",
        type_="unique",
    )
    op.drop_column("instances", "argocd_application_name")
