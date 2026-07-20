"""Coder instance creation task."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.domains import argocd
from coder_manager.models import Instance, InstanceStatus
from coder_manager.tasks._common import StatefulResourceTask

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session, sessionmaker

    from coder_manager.tasks._common import JobResult


@celery_app.task(
    name="coder_manager.create_instance",
    base=StatefulResourceTask,
    resource_type="instance",
    expected_action="creating",
)
def create_instance(instance_id: str) -> JobResult:
    """Create or attach the Argo CD Application for one Coder instance."""

    return _create_instance(UUID(instance_id), worker_database.get_worker_session_maker())


def _create_instance(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
    reconcile: Callable[[UUID, str | None, tuple[tuple[str, str], ...], str, str], str]
    | None = None,
) -> JobResult:
    """Claim and reconcile the initial Argo CD Application for an instance."""

    # Atomically claim the pending operation before contacting Argo CD.
    claimed, attached_name, region, environment = _claim_creation(instance_id, session_factory)
    if not claimed:
        return {"status": "noop"}

    # Keep the slow remote reconciliation outside the database transaction.
    try:
        reconcile_operation = reconcile or argocd.reconcile_instance_application
        application_name = reconcile_operation(
            instance_id,
            attached_name,
            (),
            region,
            environment,
        )
    except Exception:
        _mark_creation_error(instance_id, session_factory)
        raise

    # Persist the result only if this worker still owns the current action.
    with session_factory() as session:
        instance = session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if (
            instance is None
            or instance.action != "creating"
            or instance.status is not InstanceStatus.RUNNING
        ):
            return {"status": "noop"}
        instance.argocd_application_name = application_name
        instance.status = InstanceStatus.SUCCESS
        session.commit()
    return {"status": "success"}


def _claim_creation(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
) -> tuple[bool, str | None, str, str]:
    """Move an eligible creation operation to running and return its attachment."""

    with session_factory() as session:
        instance = session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if (
            instance is None
            or instance.action != "creating"
            or instance.status is not InstanceStatus.PENDING
        ):
            return False, None, "", ""
        instance.status = InstanceStatus.RUNNING
        attached_name = instance.argocd_application_name
        region = instance.region.value
        environment = instance.environment.value
        session.commit()
        return True, attached_name, region, environment


def _mark_creation_error(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
) -> None:
    """Mark the still-current creation operation as failed."""

    with session_factory() as session:
        instance = session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if (
            instance is not None
            and instance.action == "creating"
            and instance.status is InstanceStatus.RUNNING
        ):
            instance.status = InstanceStatus.ERROR
            session.commit()
