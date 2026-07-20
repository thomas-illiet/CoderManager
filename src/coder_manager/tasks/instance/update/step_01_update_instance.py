"""Reconcile one instance update and its pending members."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import select

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.domains import argocd
from coder_manager.models import (
    Instance,
    InstanceStatus,
    JobExecution,
    JobStatus,
    Member,
    MemberStatus,
)
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    fail_execution,
    owned_execution,
    required_resource_id,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import (
    INSTANCE_UPDATE_STEP_01,
    INSTANCE_UPDATE_STEP_01_TASK,
    dispatch_registered_step,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session, sessionmaker


@celery_app.task(name=INSTANCE_UPDATE_STEP_01_TASK)
def step_01_update_instance(job_id: str) -> dict[str, str]:
    """Reconcile one deterministic member snapshot and coalesce later changes."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Claim members, reconcile Argo CD, and finalize the pass."""

        member_ids, members, attached_name, region, environment = _claim_members(
            claim,
            session_factory,
        )
        try:
            application_name = argocd.reconcile_instance_application(
                required_resource_id(claim),
                attached_name,
                members,
                region,
                environment,
            )
        except Exception:
            fail_execution(
                claim,
                session_factory,
                mutate=lambda session: _fail_members(session, member_ids),
            )
            raise
        return _finalize_update(claim, member_ids, application_name, session_factory)

    return run_claimed_step(job_id, INSTANCE_UPDATE_STEP_01_TASK, session_factory, operation)


def _claim_members(
    claim: ExecutionClaim,
    session_factory: sessionmaker[Session],
) -> tuple[tuple[UUID, ...], tuple[tuple[str, str], ...], str | None, str, str]:
    """Claim the currently pending or failed member changes."""

    with session_factory() as session:
        instance = session.get(Instance, claim.resource_id)
        if instance is None:
            msg = "Instance is missing"
            raise RuntimeError(msg)
        stored_members = list(
            session.scalars(
                select(Member)
                .where(Member.instance_id == instance.id)
                .order_by(Member.username, Member.id)
                .with_for_update()
            )
        )
        claimed_ids = []
        for member in stored_members:
            if member.status in {
                MemberStatus.PENDING,
                MemberStatus.RUNNING,
                MemberStatus.ERROR,
            }:
                member.status = MemberStatus.RUNNING
                claimed_ids.append(member.id)
        active_members = tuple(
            (member.username, member.role.value)
            for member in stored_members
            if member.action != "deleting"
        )
        session.commit()
        return (
            tuple(claimed_ids),
            active_members,
            instance.argocd_application_name,
            instance.region.value,
            instance.environment.value,
        )


def _fail_members(session: Session, member_ids: tuple[UUID, ...]) -> None:
    """Mark only members owned by the failed attempt as error."""

    if not member_ids:
        return
    members = session.scalars(
        select(Member).where(
            Member.id.in_(member_ids),
            Member.status == MemberStatus.RUNNING,
        )
    )
    for member in members:
        member.status = MemberStatus.ERROR


def _finalize_update(
    claim: ExecutionClaim,
    member_ids: tuple[UUID, ...],
    application_name: str,
    session_factory: sessionmaker[Session],
) -> dict[str, str]:
    """Finalize the snapshot and create a new job when later changes are pending."""

    next_job_id = None
    with session_factory() as session:
        owned = owned_execution(session, claim)
        if owned is None:
            return {"status": "noop"}
        job, instance = owned
        if not isinstance(instance, Instance):
            return {"status": "noop"}
        instance.argocd_application_name = application_name
        if member_ids:
            members = session.scalars(
                select(Member)
                .where(
                    Member.id.in_(member_ids),
                    Member.instance_id == instance.id,
                    Member.status == MemberStatus.RUNNING,
                )
                .with_for_update()
            )
            for member in members:
                if member.action == "deleting":
                    session.delete(member)
                else:
                    member.status = MemberStatus.SUCCESS

        pending_member = session.scalar(
            select(Member.id)
            .where(
                Member.instance_id == instance.id,
                Member.status == MemberStatus.PENDING,
            )
            .limit(1)
        )
        job.status = JobStatus.SUCCESS
        job.claimed_at = None
        if pending_member is None:
            instance.status = InstanceStatus.SUCCESS
            instance.step = None
            result = {"status": "success"}
        else:
            next_job_id = uuid4()
            next_job = JobExecution(
                id=next_job_id,
                name="instance.update",
                task_name=INSTANCE_UPDATE_STEP_01_TASK,
                resource_type="instance",
                resource_id=instance.id,
                step=INSTANCE_UPDATE_STEP_01,
                status=JobStatus.PENDING,
            )
            session.add(next_job)
            instance.job_id = next_job_id
            instance.step = INSTANCE_UPDATE_STEP_01
            instance.status = InstanceStatus.PENDING
            result = {"status": "pending"}
        session.commit()
    if next_job_id is not None:
        dispatch_registered_step(INSTANCE_UPDATE_STEP_01_TASK, next_job_id)
    return result
