"""Create or adopt the Argo CD Application for a new instance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.domains import argocd
from coder_manager.models import Instance, Member
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    advance_execution,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import (
    INSTANCE_CREATE_STEP_02_TASK,
    INSTANCE_CREATE_STEP_03,
    INSTANCE_CREATE_STEP_03_TASK,
)
from coder_manager.tasks.instance._database import instance_helm_values

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@celery_app.task(name=INSTANCE_CREATE_STEP_02_TASK)
def step_02_create_instance(job_id: str) -> dict[str, str]:
    """Reconcile the remote instance and finish the creation job."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Reconcile Argo CD and finalize the instance."""

        with session_factory() as session:
            instance = session.get(Instance, claim.resource_id)
            if instance is None:
                msg = "Instance is missing"
                raise TypeError(msg)
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
            region = instance.region.value
            environment = instance.environment.value
            public_url = instance.instance_url

        helm_values = instance_helm_values(
            instance_id,
            region,
            environment,
            public_url,
            session_factory,
        )
        application_name = argocd.reconcile_instance_application(
            instance_id,
            slug,
            attached_name,
            tuple((username, role.value) for username, role in members),
            helm_values,
        )

        def store_name(_session: Session, resource: object | None) -> None:
            """Persist the deterministic Argo CD Application name."""

            if not isinstance(resource, Instance):
                msg = "Instance is missing"
                raise TypeError(msg)
            resource.argocd_application_name = application_name

        advanced = advance_execution(
            claim,
            next_task_name=INSTANCE_CREATE_STEP_03_TASK,
            next_step=INSTANCE_CREATE_STEP_03,
            session_factory=session_factory,
            mutate=store_name,
        )
        return {"status": "pending" if advanced else "noop"}

    return run_claimed_step(job_id, INSTANCE_CREATE_STEP_02_TASK, session_factory, operation)
