"""Coder instance deletion task."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import delete, select

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.domains import argocd
from coder_manager.models import (
    DatabaseAllocation,
    Instance,
    InstanceKubernetes,
    InstanceStatus,
    Member,
    Workspace,
)
from coder_manager.tasks._common import StatefulResourceTask

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session, sessionmaker

    from coder_manager.tasks._common import JobResult


@celery_app.task(
    name="coder_manager.delete_instance",
    base=StatefulResourceTask,
    resource_type="instance",
    expected_action="deleting",
)
def delete_instance(instance_id: str) -> JobResult:
    """Delete one Coder instance and all of its database-owned resources."""

    return _delete_instance(UUID(instance_id), worker_database.get_worker_session_maker())


def _delete_instance(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
    delete_application: Callable[[UUID, str | None], None] | None = None,
) -> JobResult:
    """Claim and execute one instance deletion operation."""

    # Claim the transition so duplicate deliveries become harmless no-ops.
    claimed, attached_name = _claim_deletion(instance_id, session_factory)
    if not claimed:
        return {"status": "noop"}

    # Run external cleanup without holding the instance row lock.
    try:
        delete_operation = delete_application or argocd.delete_instance_application
        delete_operation(instance_id, attached_name)
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
        session.execute(
            delete(InstanceKubernetes).where(InstanceKubernetes.instance_id == instance_id)
        )
        session.delete(instance)
        session.commit()
    return {"status": "deleted"}


def _claim_deletion(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
) -> tuple[bool, str | None]:
    """Move an eligible deletion operation to running and return its attachment."""

    with session_factory() as session:
        instance = session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if (
            instance is None
            or instance.action != "deleting"
            or instance.status is not InstanceStatus.PENDING
        ):
            return False, None
        instance.status = InstanceStatus.RUNNING
        attached_name = instance.argocd_application_name
        session.commit()
        return True, attached_name


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
