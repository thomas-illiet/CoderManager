"""Create per-instance Kubernetes provider configuration.

Revision ID: 20260720_0010
Revises: 20260719_0009
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260720_0010"
down_revision: str | None = "20260719_0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the one-to-one Kubernetes provider table."""

    op.create_table(
        "instance_kubernetes",
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("host", sa.String(length=2048), nullable=False),
        sa.Column("namespace", sa.String(length=63), nullable=False),
        sa.Column("token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("ca", sa.Text(), nullable=False),
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
            "length(trim(host)) > 0",
            name=op.f("ck_instance_kubernetes_host_not_empty"),
        ),
        sa.CheckConstraint(
            "length(trim(namespace)) > 0",
            name=op.f("ck_instance_kubernetes_namespace_not_empty"),
        ),
        sa.CheckConstraint(
            "length(trim(ca)) > 0",
            name=op.f("ck_instance_kubernetes_ca_not_empty"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["instances.id"],
            name=op.f("fk_instance_kubernetes_instance_id_instances"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("instance_id", name=op.f("pk_instance_kubernetes")),
    )


def downgrade() -> None:
    """Drop the Kubernetes provider table."""

    op.drop_table("instance_kubernetes")
