"""Stop active Coder workspaces before removing an instance Application."""

from __future__ import annotations

from typing import TYPE_CHECKING

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.config import get_settings
from coder_manager.domains import argocd, coder
from coder_manager.models import Instance
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    advance_execution,
    heartbeat_execution,
    required_resource_id,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import (
    INSTANCE_STOP_STEP_01_TASK,
    INSTANCE_STOP_STEP_02,
    INSTANCE_STOP_STEP_02_TASK,
)
from coder_manager.tasks.instance._bootstrap import stored_admin_password

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker


@celery_app.task(name=INSTANCE_STOP_STEP_01_TASK)
def step_01_stop_workspaces(job_id: str) -> dict[str, str]:
    """Stop all active remote workspaces before Application deletion."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Converge remote workspaces and advance only after every stop succeeds."""

        with session_factory() as session:
            instance = session.get(Instance, claim.resource_id)
            if instance is None:
                msg = "Instance is missing"
                raise RuntimeError(msg)
            slug = instance.slug
            attached_name = instance.argocd_application_name

        if argocd.instance_application_exists(slug, attached_name):
            credentials = stored_admin_password(
                required_resource_id(claim),
                session_factory,
            )
            if credentials is None:
                msg = "Instance administrator password is missing"
                raise RuntimeError(msg)
            settings = get_settings()
            instance_url, password = credentials
            coder.stop_active_workspaces(
                instance_url,
                password,
                timeout_seconds=settings.workspace_stop_timeout_seconds,
                poll_interval_seconds=settings.workspace_stop_poll_interval_seconds,
                heartbeat=lambda: _heartbeat_owned(claim, session_factory),
            )

        advanced = advance_execution(
            claim,
            next_task_name=INSTANCE_STOP_STEP_02_TASK,
            next_step=INSTANCE_STOP_STEP_02,
            session_factory=session_factory,
        )
        return {"status": "pending" if advanced else "noop"}

    return run_claimed_step(job_id, INSTANCE_STOP_STEP_01_TASK, session_factory, operation)


def _heartbeat_owned(
    claim: ExecutionClaim,
    session_factory: sessionmaker[Session],
) -> None:
    """Abort remote work when the current stop attempt loses ownership."""

    if not heartbeat_execution(claim, session_factory):
        msg = "Instance stop attempt is no longer current"
        raise RuntimeError(msg)
