"""Create durable job executions and resource step tracking.

Revision ID: 20260720_0011
Revises: 20260720_0010
"""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260720_0011"
down_revision: str | None = "20260720_0010"
branch_labels: str | None = None
depends_on: str | None = None

INSTANCE_STEPS = {
    "creating": (
        "instance.create",
        "coder_manager.instance.create.step_01_create_schema",
        "step_01_create_schema",
    ),
    "updating": (
        "instance.update",
        "coder_manager.instance.update.step_01_update_instance",
        "step_01_update_instance",
    ),
    "deleting": (
        "instance.delete",
        "coder_manager.instance.delete.step_01_remove_workspaces",
        "step_01_remove_workspaces",
    ),
}
WORKSPACE_STEPS = {
    "creating": (
        "workspace.create",
        "coder_manager.workspace.create.step_01_create_workspace",
        "step_01_create_workspace",
    ),
    "updating": (
        "workspace.update",
        "coder_manager.workspace.update.step_01_update_workspace",
        "step_01_update_workspace",
    ),
    "deleting": (
        "workspace.delete",
        "coder_manager.workspace.delete.step_01_delete_workspace",
        "step_01_delete_workspace",
    ),
}


def _backfill_active_resources() -> None:
    """Create pending jobs for known non-terminal resource actions."""

    connection = op.get_bind()
    jobs = sa.table(
        "job_executions",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("task_name", sa.String()),
        sa.column("resource_type", sa.String()),
        sa.column("resource_id", sa.Uuid()),
        sa.column("step", sa.String()),
        sa.column(
            "status",
            postgresql.ENUM(
                "pending",
                "running",
                "success",
                "error",
                name="job_status",
                create_type=False,
            ),
        ),
        sa.column("attempt", sa.Integer()),
    )
    for table_name, resource_type, mapping in (
        ("instances", "instance", INSTANCE_STEPS),
        ("workspaces", "workspace", WORKSPACE_STEPS),
    ):
        resource_status = postgresql.ENUM(
            "pending",
            "running",
            "success",
            "error",
            name=f"{resource_type}_status",
            create_type=False,
        )
        resources = sa.table(
            table_name,
            sa.column("id", sa.Uuid()),
            sa.column("action", sa.String()),
            sa.column("status", resource_status),
            sa.column("job_id", sa.Uuid()),
            sa.column("step", sa.String()),
        )
        rows = connection.execute(
            sa.select(resources.c.id, resources.c.action).where(resources.c.status != "success")
        )
        for resource_id, action in rows:
            definition = mapping.get(action)
            if definition is None:
                continue
            name, task_name, step = definition
            job_id = uuid4()
            connection.execute(
                jobs.insert().values(
                    id=job_id,
                    name=name,
                    task_name=task_name,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    step=step,
                    status="pending",
                    attempt=0,
                )
            )
            connection.execute(
                resources.update()
                .where(resources.c.id == resource_id)
                .values(job_id=job_id, step=step, status="pending")
            )


def upgrade() -> None:
    """Create durable job storage and backfill active resources."""

    job_status = sa.Enum("pending", "running", "success", "error", name="job_status")
    op.create_table(
        "job_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("task_name", sa.String(length=255), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
        sa.Column("step", sa.String(length=255), nullable=False),
        sa.Column("status", job_status, server_default="pending", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("attempt >= 0", name=op.f("ck_job_executions_attempt_non_negative")),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_job_executions_name_not_empty")),
        sa.CheckConstraint("length(trim(step)) > 0", name=op.f("ck_job_executions_step_not_empty")),
        sa.CheckConstraint(
            "length(trim(task_name)) > 0", name=op.f("ck_job_executions_task_name_not_empty")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_executions")),
    )
    op.create_index(
        "ix_job_executions_retry",
        "job_executions",
        ["status", "claimed_at"],
        unique=False,
    )
    for table_name in ("instances", "workspaces"):
        op.add_column(table_name, sa.Column("job_id", sa.Uuid(), nullable=True))
        op.add_column(table_name, sa.Column("step", sa.String(length=255), nullable=True))
        op.create_index(op.f(f"ix_{table_name}_job_id"), table_name, ["job_id"], unique=False)
        op.create_foreign_key(
            op.f(f"fk_{table_name}_job_id_job_executions"),
            table_name,
            "job_executions",
            ["job_id"],
            ["id"],
            ondelete="SET NULL",
        )
    _backfill_active_resources()


def downgrade() -> None:
    """Remove durable job storage and resource step tracking."""

    for table_name in ("workspaces", "instances"):
        op.drop_constraint(
            op.f(f"fk_{table_name}_job_id_job_executions"), table_name, type_="foreignkey"
        )
        op.drop_index(op.f(f"ix_{table_name}_job_id"), table_name=table_name)
        op.drop_column(table_name, "step")
        op.drop_column(table_name, "job_id")
    op.drop_index("ix_job_executions_retry", table_name="job_executions")
    op.drop_table("job_executions")
    if op.get_bind().dialect.name == "postgresql":
        sa.Enum(name="job_status").drop(op.get_bind(), checkfirst=True)
