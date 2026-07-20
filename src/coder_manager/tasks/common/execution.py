"""Transactional ownership helpers for durable task steps."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from coder_manager.models import (
    Instance,
    InstanceStatus,
    JobExecution,
    JobStatus,
    Workspace,
    WorkspaceStatus,
)
from coder_manager.tasks.common.registry import dispatch_registered_step

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

RESOURCE_ACTIONS = {
    "instance.create": "creating",
    "instance.update": "updating",
    "instance.delete": "deleting",
    "workspace.create": "creating",
    "workspace.update": "updating",
    "workspace.delete": "deleting",
}


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    """Ownership token fencing one concrete execution attempt."""

    job_id: UUID
    task_name: str
    step: str
    attempt: int
    resource_type: str | None
    resource_id: UUID | None


def _resource_for_job(
    session: Session,
    job: JobExecution,
    *,
    lock: bool,
) -> Instance | Workspace | None:
    """Load the resource attached to a job, optionally locking it."""

    if job.resource_type is None or job.resource_id is None:
        return None
    model = {"instance": Instance, "workspace": Workspace}.get(job.resource_type)
    if model is None:
        return None
    statement = select(model).where(model.id == job.resource_id)
    if lock:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _resource_matches(job: JobExecution, resource: Instance | Workspace | None) -> bool:
    """Check that the current resource still owns this exact job step."""

    if job.resource_type is None:
        return True
    if resource is None:
        return False
    expected_action = RESOURCE_ACTIONS.get(job.name)
    return (
        expected_action is not None
        and resource.job_id == job.id
        and resource.action == expected_action
        and resource.step == job.step
    )


def claim_execution(
    job_id: UUID,
    task_name: str,
    session_factory: sessionmaker[Session],
) -> ExecutionClaim | None:
    """Claim one pending or failed execution under job and resource locks."""

    with session_factory() as session:
        job = session.scalar(
            select(JobExecution).where(JobExecution.id == job_id).with_for_update()
        )
        if (
            job is None
            or job.task_name != task_name
            or job.status not in {JobStatus.PENDING, JobStatus.ERROR}
        ):
            return None
        resource = _resource_for_job(session, job, lock=True)
        if not _resource_matches(job, resource):
            return None
        job.status = JobStatus.RUNNING
        job.attempt += 1
        job.claimed_at = datetime.now(UTC)
        job.updated_at = datetime.now(UTC)
        if isinstance(resource, Instance):
            resource.status = InstanceStatus.RUNNING
        elif isinstance(resource, Workspace):
            resource.status = WorkspaceStatus.RUNNING
        claim = ExecutionClaim(
            job_id=job.id,
            task_name=job.task_name,
            step=job.step,
            attempt=job.attempt,
            resource_type=job.resource_type,
            resource_id=job.resource_id,
        )
        session.commit()
        return claim


def owned_execution(
    session: Session,
    claim: ExecutionClaim,
) -> tuple[JobExecution, Instance | Workspace | None] | None:
    """Lock and return a job/resource pair only while the claim still owns it."""

    job = session.scalar(
        select(JobExecution).where(JobExecution.id == claim.job_id).with_for_update()
    )
    if (
        job is None
        or job.task_name != claim.task_name
        or job.step != claim.step
        or job.attempt != claim.attempt
        or job.status is not JobStatus.RUNNING
    ):
        return None
    resource = _resource_for_job(session, job, lock=True)
    if not _resource_matches(job, resource):
        return None
    return job, resource


def advance_execution(
    claim: ExecutionClaim,
    *,
    next_task_name: str,
    next_step: str,
    session_factory: sessionmaker[Session],
) -> bool:
    """Persist a next pending step, then ask Celery to execute it."""

    with session_factory() as session:
        owned = owned_execution(session, claim)
        if owned is None:
            return False
        job, resource = owned
        job.task_name = next_task_name
        job.step = next_step
        job.status = JobStatus.PENDING
        job.claimed_at = None
        job.updated_at = datetime.now(UTC)
        if isinstance(resource, Instance):
            resource.step = next_step
            resource.status = InstanceStatus.PENDING
        elif isinstance(resource, Workspace):
            resource.step = next_step
            resource.status = WorkspaceStatus.PENDING
        session.commit()
    dispatch_registered_step(next_task_name, claim.job_id)
    return True


def complete_execution(
    claim: ExecutionClaim,
    session_factory: sessionmaker[Session],
    *,
    mutate: Callable[[Session, Instance | Workspace | None], None] | None = None,
    delete_resource: bool = False,
) -> bool:
    """Complete an owned execution and optionally mutate or delete its resource."""

    with session_factory() as session:
        owned = owned_execution(session, claim)
        if owned is None:
            return False
        job, resource = owned
        if mutate is not None:
            mutate(session, resource)
        job.status = JobStatus.SUCCESS
        job.claimed_at = None
        job.updated_at = datetime.now(UTC)
        if resource is not None:
            if delete_resource:
                session.delete(resource)
            elif isinstance(resource, Instance):
                resource.status = InstanceStatus.SUCCESS
                resource.step = None
            elif isinstance(resource, Workspace):
                resource.status = WorkspaceStatus.SUCCESS
                resource.step = None
        session.commit()
        return True


def fail_execution(
    claim: ExecutionClaim,
    session_factory: sessionmaker[Session],
    *,
    mutate: Callable[[Session], None] | None = None,
) -> None:
    """Fail only the still-owned attempt and its attached resource."""

    with session_factory() as session:
        owned = owned_execution(session, claim)
        if owned is None:
            return
        job, resource = owned
        if mutate is not None:
            mutate(session)
        job.status = JobStatus.ERROR
        job.claimed_at = None
        job.updated_at = datetime.now(UTC)
        if isinstance(resource, Instance):
            resource.status = InstanceStatus.ERROR
        elif isinstance(resource, Workspace):
            resource.status = WorkspaceStatus.ERROR
        session.commit()


def run_claimed_step(
    job_id: str,
    task_name: str,
    session_factory: sessionmaker[Session],
    operation: Callable[[ExecutionClaim], dict[str, str]],
) -> dict[str, str]:
    """Claim a step, run it, and persist terminal failures around its whole body."""

    claim = claim_execution(UUID(job_id), task_name, session_factory)
    if claim is None:
        return {"status": "noop"}
    try:
        return operation(claim)
    except Exception:
        fail_execution(claim, session_factory)
        raise


def prepare_execution_retry(
    job_id: UUID,
    *,
    stale_before: datetime,
    session_factory: sessionmaker[Session],
) -> str | None:
    """Return a retryable task name and reclaim an expired running attempt."""

    with session_factory() as session:
        job = session.scalar(
            select(JobExecution).where(JobExecution.id == job_id).with_for_update()
        )
        if job is None or job.status is JobStatus.SUCCESS:
            return None
        resource = _resource_for_job(session, job, lock=True)
        if not _resource_matches(job, resource):
            return None
        if job.status is JobStatus.RUNNING:
            claimed_at = job.claimed_at
            if claimed_at is not None and claimed_at.tzinfo is None:
                claimed_at = claimed_at.replace(tzinfo=UTC)
            if claimed_at is None or claimed_at > stale_before:
                return None
            job.status = JobStatus.PENDING
            job.claimed_at = None
            if isinstance(resource, Instance):
                resource.status = InstanceStatus.PENDING
            elif isinstance(resource, Workspace):
                resource.status = WorkspaceStatus.PENDING
            session.commit()
        return job.task_name


def required_resource_id(claim: ExecutionClaim) -> UUID:
    """Return the resource UUID required by a resource-bound task step."""

    if claim.resource_id is None:
        msg = "Job resource is missing"
        raise RuntimeError(msg)
    return claim.resource_id
