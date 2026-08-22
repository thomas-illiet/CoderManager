"""Shared synchronous helpers for remote Coder workspace lifecycle."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from coder_manager.config import get_settings
from coder_manager.crypto import InstancePasswordCipher
from coder_manager.domains.coder import (
    CoderClient,
    CoderRequestError,
    CoderWorkspace,
    CoderWorkspaceBuild,
)
from coder_manager.models import (
    Instance,
    Member,
    TemplateDeployment,
    TemplateParameter,
    TemplateParameterType,
    Workspace,
)
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    complete_execution,
    owned_execution,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from pydantic import SecretStr
    from sqlalchemy.orm import Session, sessionmaker

    from coder_manager.utils.instance_urls import InstancePublicUrlConfig


class WorkspaceRemoteError(Exception):
    """Raised when a remote workspace cannot be reconciled safely."""


@dataclass(frozen=True, slots=True)
class WorkspaceRemoteSnapshot:
    """Local state required to converge one remote workspace."""

    id: UUID
    name: str
    username: str
    instance_url: str
    password: SecretStr
    coder_template_id: UUID
    coder_workspace_id: UUID | None
    coder_workspace_build_id: UUID | None
    parameters: tuple[tuple[str, str], ...]
    parameters_revision: int
    applied_parameters_revision: int | None


def workspace_remote_snapshot(
    workspace_id: UUID,
    session_factory: sessionmaker[Session],
    url_config: InstancePublicUrlConfig,
) -> WorkspaceRemoteSnapshot:
    """Load and decrypt one stable workspace reconciliation snapshot."""

    with session_factory() as session:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            msg = "Workspace is missing"
            raise WorkspaceRemoteError(msg)
        instance = session.get(Instance, workspace.instance_id)
        member = session.get(Member, workspace.member_id)
        deployment = session.scalar(
            select(TemplateDeployment).where(
                TemplateDeployment.template_id == workspace.template_id,
                TemplateDeployment.instance_id == workspace.instance_id,
            )
        )
        if (
            instance is None
            or member is None
            or deployment is None
            or deployment.coder_template_id is None
            or instance.password_enc is None
        ):
            msg = "Workspace remote prerequisites are missing"
            raise WorkspaceRemoteError(msg)
        active_names = set(
            session.scalars(
                select(TemplateParameter.name).where(
                    TemplateParameter.template_id == workspace.template_id,
                    TemplateParameter.type == TemplateParameterType.USER,
                )
            )
        )
        parameters = tuple(
            sorted(
                (name, value)
                for name, value in workspace.parameters.items()
                if name in active_names
            )
        )
        password = InstancePasswordCipher(get_settings().crypto_key).decrypt(
            instance.password_enc,
            instance.id,
        )
        return WorkspaceRemoteSnapshot(
            id=workspace.id,
            name=workspace.name,
            username=member.username,
            instance_url=url_config.url_for(instance.slug, instance.environment),
            password=password,
            coder_template_id=deployment.coder_template_id,
            coder_workspace_id=workspace.coder_workspace_id,
            coder_workspace_build_id=workspace.coder_workspace_build_id,
            parameters=parameters,
            parameters_revision=workspace.parameters_revision,
            applied_parameters_revision=workspace.applied_parameters_revision,
        )


def find_remote_workspace(
    client: CoderClient,
    snapshot: WorkspaceRemoteSnapshot,
) -> CoderWorkspace | None:
    """Find a remote workspace by persisted ID or exact owner/name."""

    remote = (
        client.workspace(snapshot.coder_workspace_id)
        if snapshot.coder_workspace_id is not None
        else None
    )
    if remote is None:
        remote = client.workspace_by_owner_and_name(snapshot.username, snapshot.name)
    return remote


def require_matching_template(
    remote: CoderWorkspace,
    snapshot: WorkspaceRemoteSnapshot,
) -> None:
    """Reject adoption of an unrelated same-name remote workspace."""

    if remote.template_id != snapshot.coder_template_id:
        msg = "Remote workspace uses a different template"
        raise WorkspaceRemoteError(msg)


def store_remote_ids(
    claim: ExecutionClaim,
    session_factory: sessionmaker[Session],
    *,
    workspace_id: UUID,
    build_id: UUID,
) -> bool:
    """Persist remote identifiers immediately while retaining ownership fencing."""

    with session_factory() as session:
        owned = owned_execution(session, claim)
        if owned is None:
            return False
        _job, resource = owned
        if not isinstance(resource, Workspace):
            return False
        resource.coder_workspace_id = workspace_id
        resource.coder_workspace_build_id = build_id
        session.commit()
        return True


def complete_workspace(  # noqa: PLR0913
    claim: ExecutionClaim,
    session_factory: sessionmaker[Session],
    *,
    workspace_id: UUID | None = None,
    build_id: UUID | None = None,
    apply_parameters: bool = False,
    delete_resource: bool = False,
) -> bool:
    """Complete one owned workspace attempt and store its remote convergence."""

    def mutate(
        _session: Session,
        resource: object,
    ) -> None:
        """Persist remote convergence fields on the owned workspace."""

        if not isinstance(resource, Workspace):
            return
        if workspace_id is not None:
            resource.coder_workspace_id = workspace_id
        if build_id is not None:
            resource.coder_workspace_build_id = build_id
        if apply_parameters:
            resource.applied_parameters_revision = resource.parameters_revision

    return complete_execution(
        claim,
        session_factory,
        mutate=mutate,
        delete_resource=delete_resource,
    )


def wait_workspace_build(  # noqa: PLR0913
    client: CoderClient,
    build: CoderWorkspaceBuild,
    *,
    success_status: str,
    timeout_seconds: int,
    poll_interval_seconds: float,
    heartbeat: Callable[[], None] | None = None,
) -> CoderWorkspaceBuild:
    """Wait for one workspace build to reach its expected terminal state."""

    deadline = time.monotonic() + timeout_seconds
    current = build
    while True:
        if current.status == success_status:
            return current
        if current.status in {"failed", "canceled", "canceling"}:
            msg = "Coder workspace build failed"
            raise CoderRequestError(msg)
        if time.monotonic() >= deadline:
            msg = "Coder workspace build timed out"
            raise CoderRequestError(msg)
        if heartbeat is not None:
            heartbeat()
        time.sleep(poll_interval_seconds)
        current = client.workspace_build(current.id)
