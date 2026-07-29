"""Reconcile a stopped instance's Argo CD Application."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.domains import argocd
from coder_manager.models import Instance, InstanceState, Member
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    advance_execution,
    defer_execution,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import (
    INSTANCE_START_STEP_01_TASK,
    INSTANCE_UPDATE_STEP_02,
    INSTANCE_UPDATE_STEP_02_TASK,
)
from coder_manager.tasks.instance._bootstrap import stored_admin_password
from coder_manager.tasks.instance._database import instance_helm_values

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@celery_app.task(name=INSTANCE_START_STEP_01_TASK)
def step_01_start_instance(job_id: str) -> dict[str, str]:
    """Strictly reconcile one instance before cleaning remote users."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Require complete local state and ensure the remote Application."""

        with session_factory() as session:
            instance = session.get(Instance, claim.resource_id)
            if instance is None:
                msg = "Instance is missing"
                raise RuntimeError(msg)
            members = tuple(
                session.execute(
                    select(Member.username, Member.role)
                    .where(Member.instance_id == instance.id, Member.action != "deleting")
                    .order_by(Member.username, Member.id)
                ).all()
            )
            instance_id = instance.id
            slug = instance.slug
            attached_name = instance.argocd_application_name
            environment = instance.environment.value
            public_url = instance.instance_url

        credentials = stored_admin_password(instance_id, session_factory)
        if credentials is None:
            msg = "Instance administrator password is missing"
            raise RuntimeError(msg)
        helm_values = instance_helm_values(
            instance_id,
            slug,
            environment,
            public_url,
            session_factory,
        )
        reconciliation = argocd.reconcile_instance_application(
            instance_id,
            slug,
            attached_name,
            tuple((username, role.value) for username, role in members),
            helm_values,
        )
        if reconciliation.status is argocd.ArgoCdMutationStatus.DEFERRED:
            deferred = defer_execution(claim, session_factory)
            return {"status": "deferred" if deferred else "noop"}

        def store_started(_session: Session, resource: object | None) -> None:
            """Persist the confirmed remote Application identity and state."""

            if not isinstance(resource, Instance):
                msg = "Instance is missing"
                raise TypeError(msg)
            resource.argocd_application_name = reconciliation.application_name
            resource.state = InstanceState.STARTED

        advanced = advance_execution(
            claim,
            next_task_name=INSTANCE_UPDATE_STEP_02_TASK,
            next_step=INSTANCE_UPDATE_STEP_02,
            session_factory=session_factory,
            mutate=store_started,
        )
        return {"status": "pending" if advanced else "noop"}

    return run_claimed_step(job_id, INSTANCE_START_STEP_01_TASK, session_factory, operation)
