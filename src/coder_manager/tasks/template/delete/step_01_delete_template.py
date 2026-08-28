"""Delete one assigned template and every workspace built from it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.config import get_settings
from coder_manager.crypto import InstancePasswordCipher
from coder_manager.domains import coder
from coder_manager.models import (
    Instance,
    InstanceState,
    InstanceStatus,
    JobStatus,
    TemplateAssignment,
    TemplateDeployment,
    Workspace,
)
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    heartbeat_execution,
    owned_execution,
    required_resource_id,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import TEMPLATE_ASSIGNMENT_DELETE_STEP_01_TASK
from coder_manager.utils.instance_urls import InstancePublicUrlConfig

if TYPE_CHECKING:
    from uuid import UUID

    from pydantic import SecretStr
    from sqlalchemy.orm import Session, sessionmaker


@dataclass(frozen=True, slots=True)
class TemplateRemovalSnapshot:
    """Stable local identities required before destructive remote cleanup."""

    assignment_id: UUID
    template_id: UUID
    instance_id: UUID
    instance_url: str
    password: SecretStr | None
    coder_template_id: UUID | None


def _removal_snapshot(
    assignment_id: UUID,
    session_factory: sessionmaker[Session],
) -> TemplateRemovalSnapshot:
    """Load and decrypt remote cleanup prerequisites for one assignment."""

    settings = get_settings()
    url_config = InstancePublicUrlConfig.from_settings(settings)
    with session_factory() as session:
        assignment = session.get(TemplateAssignment, assignment_id)
        if assignment is None:
            msg = "Template assignment is missing"
            raise RuntimeError(msg)
        instance = session.get(Instance, assignment.instance_id)
        if instance is None:
            msg = "Template assignment instance is missing"
            raise RuntimeError(msg)
        deployment = session.scalar(
            select(TemplateDeployment).where(TemplateDeployment.assignment_id == assignment.id)
        )
        coder_template_id = deployment.coder_template_id if deployment is not None else None
        password = None
        if coder_template_id is not None:
            if (
                instance.state is not InstanceState.STARTED
                or instance.status is not InstanceStatus.SUCCESS
            ):
                msg = "Coder instance is not started and successful"
                raise RuntimeError(msg)
            if instance.password_enc is None:
                msg = "Coder administrator password is not initialized"
                raise RuntimeError(msg)
            password = InstancePasswordCipher(settings.crypto_key).decrypt(
                instance.password_enc,
                instance.id,
            )
        return TemplateRemovalSnapshot(
            assignment_id=assignment.id,
            template_id=assignment.template_id,
            instance_id=instance.id,
            instance_url=url_config.url_for(instance.slug),
            password=password,
            coder_template_id=coder_template_id,
        )


def _remove_local_assignment(
    claim: ExecutionClaim,
    snapshot: TemplateRemovalSnapshot,
    session_factory: sessionmaker[Session],
) -> bool:
    """Remove local workspaces, deployment, and assignment in one final transaction."""

    with session_factory() as session:
        owned = owned_execution(session, claim)
        if owned is None:
            return False
        job, resource = owned
        if not isinstance(resource, TemplateAssignment):
            return False
        session.execute(
            delete(Workspace).where(
                Workspace.template_id == snapshot.template_id,
                Workspace.instance_id == snapshot.instance_id,
            )
        )
        session.execute(
            delete(TemplateDeployment).where(TemplateDeployment.assignment_id == resource.id)
        )
        job.status = JobStatus.SUCCESS
        job.claimed_at = None
        job.updated_at = datetime.now(UTC)
        session.delete(resource)
        session.commit()
        return True


def _heartbeat_owned(
    claim: ExecutionClaim,
    session_factory: sessionmaker[Session],
) -> None:
    """Abort remote assignment deletion after the durable claim is lost."""

    if not heartbeat_execution(claim, session_factory):
        message = "Template assignment deletion claim is no longer current"
        raise RuntimeError(message)


@celery_app.task(name=TEMPLATE_ASSIGNMENT_DELETE_STEP_01_TASK)
def step_01_delete_template(job_id: str) -> dict[str, str]:
    """Delete remote workspaces, the remote template, then all local assignment rows."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Converge remote deletion before committing irreversible local cleanup."""

        snapshot = _removal_snapshot(required_resource_id(claim), session_factory)

        def heartbeat() -> None:
            """Keep the durable deletion claim alive during remote workspace builds."""

            _heartbeat_owned(claim, session_factory)

        if snapshot.coder_template_id is not None and snapshot.password is not None:
            settings = get_settings()
            coder.delete_template_workspaces(
                snapshot.instance_url,
                snapshot.password,
                snapshot.coder_template_id,
                timeout_seconds=settings.workspace_delete_timeout_seconds,
                poll_interval_seconds=settings.workspace_delete_poll_interval_seconds,
                heartbeat=heartbeat,
            )
            heartbeat()
            with coder.CoderClient(snapshot.instance_url) as client:
                client.authenticate_prepared_admin(snapshot.password)
                client.delete_template(snapshot.coder_template_id)

        deleted = _remove_local_assignment(claim, snapshot, session_factory)
        return {"status": "deleted" if deleted else "noop"}

    return run_claimed_step(
        job_id,
        TEMPLATE_ASSIGNMENT_DELETE_STEP_01_TASK,
        session_factory,
        operation,
    )
