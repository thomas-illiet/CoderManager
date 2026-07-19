"""Create template images and workspaces.

Revision ID: 20260719_0007
Revises: 20260719_0006
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260719_0007"
down_revision: str | None = "20260719_0006"
branch_labels: str | None = None
depends_on: str | None = None

status_enum = sa.Enum("pending", "running", "success", "error", name="workspace_status")


def upgrade() -> None:
    """Create immutable template images and managed workspaces."""

    op.create_table(
        "template_images",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("registry", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("version", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(registry)) > 0",
            name=op.f("ck_template_images_registry_not_empty"),
        ),
        sa.CheckConstraint(
            "registry = lower(trim(registry))",
            name=op.f("ck_template_images_registry_normalized"),
        ),
        sa.CheckConstraint(
            "length(trim(name)) > 0",
            name=op.f("ck_template_images_name_not_empty"),
        ),
        sa.CheckConstraint(
            "name = lower(trim(name))",
            name=op.f("ck_template_images_name_normalized"),
        ),
        sa.CheckConstraint(
            "length(trim(version)) > 0",
            name=op.f("ck_template_images_version_not_empty"),
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["templates.id"],
            name=op.f("fk_template_images_template_id_templates"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_template_images")),
        sa.UniqueConstraint(
            "template_id",
            "registry",
            "name",
            "version",
            name=op.f("uq_template_images_reference"),
        ),
    )
    op.create_index(
        op.f("ix_template_images_template_id"),
        "template_images",
        ["template_id"],
        unique=False,
    )

    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("image_id", sa.Uuid(), nullable=False),
        sa.Column("modules", sa.JSON(), nullable=False),
        sa.Column("cpu", sa.Integer(), nullable=False),
        sa.Column("ram", sa.Integer(), nullable=False),
        sa.Column("disk", sa.Integer(), nullable=False),
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
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_workspaces_name_not_empty")),
        sa.CheckConstraint("length(trim(action)) > 0", name=op.f("ck_workspaces_action_not_empty")),
        sa.CheckConstraint("cpu > 0", name=op.f("ck_workspaces_cpu_positive")),
        sa.CheckConstraint("ram > 0", name=op.f("ck_workspaces_ram_positive")),
        sa.CheckConstraint("disk > 0", name=op.f("ck_workspaces_disk_positive")),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["instances.id"],
            name=op.f("fk_workspaces_instance_id_instances"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["templates.id"],
            name=op.f("fk_workspaces_template_id_templates"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["members.id"],
            name=op.f("fk_workspaces_member_id_members"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["image_id"],
            ["template_images.id"],
            name=op.f("fk_workspaces_image_id_template_images"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
    )
    for column in ("instance_id", "template_id", "member_id", "image_id"):
        op.create_index(op.f(f"ix_workspaces_{column}"), "workspaces", [column], unique=False)
    op.create_index(
        "uq_workspaces_instance_name_ci",
        "workspaces",
        ["instance_id", sa.text("lower(name)")],
        unique=True,
    )


def downgrade() -> None:
    """Drop workspaces, template images, and their status type."""

    op.drop_index("uq_workspaces_instance_name_ci", table_name="workspaces")
    for column in ("image_id", "member_id", "template_id", "instance_id"):
        op.drop_index(op.f(f"ix_workspaces_{column}"), table_name="workspaces")
    op.drop_table("workspaces")
    op.drop_index(op.f("ix_template_images_template_id"), table_name="template_images")
    op.drop_table("template_images")
    status_enum.drop(op.get_bind(), checkfirst=True)
