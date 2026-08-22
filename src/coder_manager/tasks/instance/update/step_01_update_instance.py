"""Reconcile one instance update and its pending members."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import select

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.config import get_settings
from coder_manager.domains import argocd
from coder_manager.models import (
    Instance,
    InstanceState,
    InstanceStatus,
    JobExecution,
    JobStatus,
    Member,
    MemberStatus,
)
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    advance_execution,
    defer_execution,
    fail_execution,
    owned_execution,
    required_resource_id,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import (
    INSTANCE_UPDATE_STEP_01,
    INSTANCE_UPDATE_STEP_01_TASK,
    INSTANCE_UPDATE_STEP_02,
    INSTANCE_UPDATE_STEP_02_TASK,
    dispatch_registered_step,
)
from coder_manager.tasks.instance._database import instance_helm_values
from coder_manager.utils.instance_urls import InstancePublicUrlConfig

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session, sessionmaker


@celery_app.task(name=INSTANCE_UPDATE_STEP_01_TASK)
def step_01_update_instance(job_id: str) -> dict[str, str]:
    """Reconcile one deterministic member snapshot and coalesce later changes."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Claim members, reconcile Argo CD, and finalize the pass."""

        settings = get_settings()
        url_config = InstancePublicUrlConfig.from_settings(settings)
        (
            member_ids,
            members,
            slug,
            attached_name,
            environment,
            public_url,
        ) = _claim_members(claim, session_factory, url_config)
        try:
            helm_values = instance_helm_values(
                required_resource_id(claim),
                slug,
                environment,
                public_url,
                session_factory,
            )
            reconciliation = argocd.reconcile_instance_application(
                required_resource_id(claim),
                slug,
                attached_name,
                members,
                helm_values,
            )
        except Exception:
            fail_execution(
                claim,
                session_factory,
                mutate=lambda session: _fail_members(session, member_ids),
            )
            raise
        if reconciliation.status is argocd.ArgoCdMutationStatus.DEFERRED:
            deferred = defer_execution(
                claim,
                session_factory,
                mutate=lambda session, _resource: _defer_members(session, member_ids),
            )
            return {"status": "deferred" if deferred else "noop"}
        advanced = advance_execution(
            claim,
            next_task_name=INSTANCE_UPDATE_STEP_02_TASK,
            next_step=INSTANCE_UPDATE_STEP_02,
            session_factory=session_factory,
            mutate=lambda _session, resource: _store_application_name(
                resource,
                reconciliation.application_name,
            ),
        )
        return {"status": "pending" if advanced else "noop"}

    return run_claimed_step(job_id, INSTANCE_UPDATE_STEP_01_TASK, session_factory, operation)


def _store_application_name(
    resource: object,
    application_name: str,
) -> None:
    """Persist the reconciled Argo CD name before the cleanup step."""

    if not isinstance(resource, Instance):
        msg = "Instance update resource is missing"
        raise TypeError(msg)
    resource.argocd_application_name = application_name
    resource.state = InstanceState.STARTED


def _claim_members(
    claim: ExecutionClaim,
    session_factory: sessionmaker[Session],
    url_config: InstancePublicUrlConfig,
) -> tuple[
    tuple[UUID, ...],
    tuple[tuple[str, str], ...],
    str,
    str | None,
    str,
    str,
]:
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
            instance.slug,
            instance.argocd_application_name,
            instance.environment.value,
            url_config.url_for(instance.slug, instance.environment),
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


def _defer_members(session: Session, member_ids: tuple[UUID, ...]) -> None:
    """Return only members claimed by the deferred attempt to pending."""

    if not member_ids:
        return
    members = session.scalars(
        select(Member).where(
            Member.id.in_(member_ids),
            Member.status == MemberStatus.RUNNING,
        )
    )
    for member in members:
        member.status = MemberStatus.PENDING


def _finalize_update(
    claim: ExecutionClaim,
    member_ids: tuple[UUID, ...],
    application_name: str,
    session_factory: sessionmaker[Session],
) -> dict[str, str]:
    """Finalize the snapshot and create a new job when later changes are pending."""

    dispatch: tuple[str, UUID] | None = None
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
        if pending_member is None:
            job.status = JobStatus.SUCCESS
            job.claimed_at = None
            instance.status = InstanceStatus.SUCCESS
            instance.step = None
            result = {"status": "success"}
        else:
            job.status = JobStatus.SUCCESS
            job.claimed_at = None
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
            instance.action = "updating"
            instance.step = INSTANCE_UPDATE_STEP_01
            instance.status = InstanceStatus.PENDING
            dispatch = (INSTANCE_UPDATE_STEP_01_TASK, next_job_id)
            result = {"status": "pending"}
        session.commit()
    if dispatch is not None:
        dispatch_registered_step(*dispatch)
    return result
