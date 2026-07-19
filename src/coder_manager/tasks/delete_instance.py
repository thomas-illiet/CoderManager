"""Coder instance deletion task."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import delete, select

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.models import DatabaseAllocation, Instance, InstanceStatus, Member, Workspace
from coder_manager.tasks import _common

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from coder_manager.tasks._common import JobResult


@celery_app.task(name="coder_manager.delete_instance")
def delete_instance(instance_id: str) -> JobResult:
    """Delete one Coder instance and all of its database-owned resources."""

    return _delete_instance(UUID(instance_id), worker_database.get_worker_session_maker())


def _delete_instance(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
) -> JobResult:
    """Claim and execute one instance deletion operation."""

    # Claim the transition so duplicate deliveries become harmless no-ops.
    if not _claim_deletion(instance_id, session_factory):
        return {"status": "noop"}

    # Run external cleanup without holding the instance row lock.
    try:
        _common.placeholder()
    except Exception:
        _mark_deletion_error(instance_id, session_factory)
        raise

    # Remove dependent rows only while this worker still owns the action.
    with session_factory() as session:
        instance = session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if (
            instance is None
            or instance.action != "deleting"
            or instance.status is not InstanceStatus.RUNNING
        ):
            return {"status": "noop"}
        session.execute(delete(Workspace).where(Workspace.instance_id == instance_id))
        session.execute(delete(Member).where(Member.instance_id == instance_id))
        session.execute(
            delete(DatabaseAllocation).where(DatabaseAllocation.instance_id == instance_id)
        )
        session.delete(instance)
        session.commit()
    return {"status": "deleted"}


def _claim_deletion(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
) -> bool:
    """Move an eligible deletion operation to running."""

    with session_factory() as session:
        instance = session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if (
            instance is None
            or instance.action != "deleting"
            or instance.status is not InstanceStatus.PENDING
        ):
            return False
        instance.status = InstanceStatus.RUNNING
        session.commit()
        return True


def _mark_deletion_error(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
) -> None:
    """Mark the still-current deletion operation as failed."""

    with session_factory() as session:
        instance = session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if (
            instance is not None
            and instance.action == "deleting"
            and instance.status is InstanceStatus.RUNNING
        ):
            instance.status = InstanceStatus.ERROR
            session.commit()
