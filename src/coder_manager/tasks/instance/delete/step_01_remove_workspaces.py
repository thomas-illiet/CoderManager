"""Delete every remote workspace before instance deletion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.config import get_settings
from coder_manager.domains import argocd, coder
from coder_manager.models import Instance, InstanceState, Member
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    advance_execution,
    defer_execution,
    heartbeat_execution,
    owned_execution,
    required_resource_id,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import (
    INSTANCE_DELETE_STEP_01_TASK,
    INSTANCE_DELETE_STEP_02,
    INSTANCE_DELETE_STEP_02_TASK,
)
from coder_manager.tasks.instance._bootstrap import stored_admin_password
from coder_manager.tasks.instance._database import instance_helm_values

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker


@celery_app.task(name=INSTANCE_DELETE_STEP_01_TASK)
def step_01_remove_workspaces(job_id: str) -> dict[str, str]:
    """Delete all remote workspaces and schedule remote instance deletion."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Restore Coder when needed and prove that every workspace is deleted."""

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

        credentials = stored_admin_password(
            required_resource_id(claim),
            session_factory,
        )
        if credentials is None:
            msg = "Instance administrator password is missing"
            raise RuntimeError(msg)
        instance_url, password = credentials

        restored_name = attached_name
        if not argocd.instance_application_exists(slug, attached_name, environment):
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
            restored_name = reconciliation.application_name

        if not _store_started_application(
            claim,
            restored_name,
            session_factory,
        ):
            return {"status": "noop"}

        settings = get_settings()
        coder.delete_all_workspaces(
            instance_url,
            password,
            timeout_seconds=settings.workspace_delete_timeout_seconds,
            poll_interval_seconds=settings.workspace_delete_poll_interval_seconds,
            heartbeat=lambda: _heartbeat_owned(claim, session_factory),
        )
        advanced = advance_execution(
            claim,
            next_task_name=INSTANCE_DELETE_STEP_02_TASK,
            next_step=INSTANCE_DELETE_STEP_02,
            session_factory=session_factory,
        )
        return {"status": "pending" if advanced else "noop"}

    return run_claimed_step(job_id, INSTANCE_DELETE_STEP_01_TASK, session_factory, operation)


def _store_started_application(
    claim: ExecutionClaim,
    application_name: str | None,
    session_factory: sessionmaker[Session],
) -> bool:
    """Persist the temporary Application only while this delete attempt owns it."""

    with session_factory() as session:
        owned = owned_execution(session, claim)
        if owned is None:
            return False
        _job, resource = owned
        if not isinstance(resource, Instance):
            msg = "Instance is missing"
            raise TypeError(msg)
        if application_name is not None:
            resource.argocd_application_name = application_name
        resource.state = InstanceState.STARTED
        session.commit()
        return True


def _heartbeat_owned(
    claim: ExecutionClaim,
    session_factory: sessionmaker[Session],
) -> None:
    """Abort destructive remote work when this delete attempt loses ownership."""

    if not heartbeat_execution(claim, session_factory):
        msg = "Instance delete attempt is no longer current"
        raise RuntimeError(msg)
