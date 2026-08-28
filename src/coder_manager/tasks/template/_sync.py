"""Shared synchronous helpers for publishing explicitly assigned templates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import SecretStr
from sqlalchemy import select

from coder_manager.config import get_settings
from coder_manager.crypto import InstancePasswordCipher, TemplateParameterCipher
from coder_manager.domains.coder import CoderClient, CoderTemplate
from coder_manager.domains.template_source import TemplateArchive, fetch_branch_archive
from coder_manager.models import (
    Instance,
    InstanceState,
    InstanceStatus,
    JobExecution,
    JobStatus,
    Template,
    TemplateAssignment,
    TemplateAssignmentStatus,
    TemplateDeployment,
    TemplateDeploymentStatus,
    TemplateParameterType,
    TemplateSyncStatus,
)
from coder_manager.utils.instance_urls import InstancePublicUrlConfig

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from sqlalchemy.orm import Session, sessionmaker

    from coder_manager.tasks.common.execution import ExecutionClaim


@dataclass(frozen=True, slots=True)
class TemplateSourceSnapshot:
    """Source and display fields read atomically for one synchronization."""

    id: UUID
    display_name: str
    name: str
    git_url: str
    source_path: str
    branch: str
    system_parameter_revision: int


@dataclass(frozen=True, slots=True)
class TemplateDeploymentPreparation:
    """Local state committed before one remote publication attempt."""

    already_applied: bool
    instance_url: str
    password: SecretStr
    persisted_template_id: UUID | None
    persisted_version_id: UUID | None
    system_values: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _OwnedTemplateTarget:
    """Rows locked in the global job-to-deployment order for one claimed write."""

    instance: Instance
    template: Template
    assignment: TemplateAssignment
    deployment: TemplateDeployment | None


class TemplateTargetSyncError(Exception):
    """Raised after a sanitized target synchronization failure."""


class TemplateTargetClaimLostError(TemplateTargetSyncError):
    """Raised before a stale template worker can mutate deployment state."""


def template_source_snapshot(
    template_id: UUID,
    session_factory: sessionmaker[Session],
) -> TemplateSourceSnapshot:
    """Load the source fields required for one branch fetch."""

    with session_factory() as session:
        template = session.get(Template, template_id)
        if template is None:
            msg = "Template is missing"
            raise TemplateTargetSyncError(msg)
        return TemplateSourceSnapshot(
            id=template.id,
            display_name=template.display_name,
            name=template.name,
            git_url=template.git_url,
            source_path=template.source_path,
            branch=template.branch,
            system_parameter_revision=template.system_parameter_revision,
        )


def fetch_template_archive(snapshot: TemplateSourceSnapshot) -> TemplateArchive:
    """Fetch the configured branch using the process allowlist."""

    return fetch_branch_archive(
        snapshot.git_url,
        snapshot.branch,
        snapshot.source_path,
    )


def assignment_template_id(
    assignment_id: UUID,
    session_factory: sessionmaker[Session],
) -> UUID:
    """Return the template owned by one durable assignment job."""

    with session_factory() as session:
        template_id = session.scalar(
            select(TemplateAssignment.template_id).where(TemplateAssignment.id == assignment_id)
        )
        if template_id is None:
            msg = "Template assignment is missing"
            raise TemplateTargetSyncError(msg)
        return template_id


def stable_assignment_ids(
    template_id: UUID,
    session_factory: sessionmaker[Session],
) -> tuple[UUID, ...]:
    """Return stable assignments on started, successful instances."""

    with session_factory() as session:
        if session.get(Template, template_id) is None:
            msg = "Template is missing"
            raise TemplateTargetSyncError(msg)
        return tuple(
            session.scalars(
                select(TemplateAssignment.id)
                .join(Instance, Instance.id == TemplateAssignment.instance_id)
                .where(
                    TemplateAssignment.template_id == template_id,
                    TemplateAssignment.action == "created",
                    TemplateAssignment.status == TemplateAssignmentStatus.SUCCESS,
                    Instance.state == InstanceState.STARTED,
                    Instance.status == InstanceStatus.SUCCESS,
                    Instance.action != "deleting",
                )
                .order_by(TemplateAssignment.id)
            )
        )


def _prepare_deployment(  # noqa: PLR0913
    claim: ExecutionClaim,
    assignment_id: UUID,
    template_id: UUID,
    commit: str,
    system_parameter_revision: int,
    session_factory: sessionmaker[Session],
    url_config: InstancePublicUrlConfig,
) -> TemplateDeploymentPreparation:
    """Mark one assignment running and return persisted recovery identifiers."""

    with session_factory() as session:
        target = _lock_owned_template_target(
            session,
            claim,
            assignment_id,
            expected_template_id=template_id,
        )
        instance = target.instance
        template = target.template
        if (
            instance.state is not InstanceState.STARTED
            or instance.status is not InstanceStatus.SUCCESS
        ):
            msg = "Coder instance is not started and successful"
            raise TemplateTargetSyncError(msg)
        if instance.password_enc is None:
            msg = "Coder administrator password is not initialized"
            raise TemplateTargetSyncError(msg)
        instance_url = url_config.url_for(instance.slug)

        deployment = target.deployment
        if deployment is None:
            deployment = TemplateDeployment(assignment_id=assignment_id)
            session.add(deployment)
            session.flush()
        if (
            deployment.status is TemplateDeploymentStatus.SUCCESS
            and deployment.applied_commit == commit
            and deployment.applied_system_parameter_revision == system_parameter_revision
        ):
            return TemplateDeploymentPreparation(
                already_applied=True,
                instance_url=instance_url,
                password=SecretStr(""),
                persisted_template_id=deployment.coder_template_id,
                persisted_version_id=deployment.coder_template_version_id,
                system_values=(),
            )

        if (
            deployment.target_commit != commit
            or deployment.target_system_parameter_revision != system_parameter_revision
        ):
            deployment.coder_template_version_id = None
        deployment.target_commit = commit
        deployment.target_system_parameter_revision = system_parameter_revision
        deployment.status = TemplateDeploymentStatus.RUNNING
        settings = get_settings()
        password = InstancePasswordCipher(settings.crypto_key).decrypt(
            instance.password_enc,
            instance.id,
        )
        system_values = _system_parameter_values(
            template,
            TemplateParameterCipher(settings.crypto_key),
        )
        preparation = TemplateDeploymentPreparation(
            already_applied=False,
            instance_url=instance_url,
            password=password,
            persisted_template_id=deployment.coder_template_id,
            persisted_version_id=deployment.coder_template_version_id,
            system_values=system_values,
        )
        session.commit()
        return preparation


def _system_parameter_values(
    template: Template,
    cipher: TemplateParameterCipher,
) -> tuple[tuple[str, str], ...]:
    """Resolve and decrypt system values for one template publication."""

    resolved: list[tuple[str, str]] = []
    for parameter in sorted(template.parameters, key=lambda item: item.name):
        if parameter.type is not TemplateParameterType.SYSTEM:
            continue
        value = parameter.system_value
        if value is None:
            msg = "Template system parameter value is missing"
            raise TemplateTargetSyncError(msg)
        resolved.append(
            (
                parameter.name,
                cipher.decrypt(value.value_enc, parameter.id),
            )
        )
    return tuple(resolved)


def _store_remote_ids(  # noqa: PLR0913
    claim: ExecutionClaim,
    assignment_id: UUID,
    *,
    organization_id: UUID | None = None,
    coder_template_id: UUID | None = None,
    coder_template_version_id: UUID | None = None,
    session_factory: sessionmaker[Session],
) -> None:
    """Persist remote identifiers immediately to close retry windows."""

    with session_factory() as session:
        deployment = _lock_owned_template_target(
            session,
            claim,
            assignment_id,
        ).deployment
        if deployment is None:
            msg = "Template deployment is missing"
            raise TemplateTargetSyncError(msg)
        if organization_id is not None:
            deployment.coder_organization_id = organization_id
        if coder_template_id is not None:
            deployment.coder_template_id = coder_template_id
        if coder_template_version_id is not None:
            deployment.coder_template_version_id = coder_template_version_id
        session.commit()


def _finish_deployment(  # noqa: PLR0913
    claim: ExecutionClaim,
    assignment_id: UUID,
    commit: str,
    system_parameter_revision: int,
    *,
    success: bool,
    session_factory: sessionmaker[Session],
) -> None:
    """Store only the current success or error state for one assignment."""

    with session_factory() as session:
        deployment = _lock_owned_template_target(
            session,
            claim,
            assignment_id,
        ).deployment
        if deployment is None:
            return
        deployment.status = (
            TemplateDeploymentStatus.SUCCESS if success else TemplateDeploymentStatus.ERROR
        )
        if success:
            deployment.applied_commit = commit
            deployment.applied_system_parameter_revision = system_parameter_revision
        session.commit()


def _lock_owned_template_target(
    session: Session,
    claim: ExecutionClaim,
    assignment_id: UUID,
    *,
    expected_template_id: UUID | None = None,
) -> _OwnedTemplateTarget:
    """Fence a deployment write under job, instance, template, assignment, deployment locks."""

    job = session.scalar(
        select(JobExecution).where(JobExecution.id == claim.job_id).with_for_update()
    )
    if job is None or not _job_matches_claim(job, claim):
        raise _claim_lost()

    assignment_identity = session.execute(
        select(
            TemplateAssignment.instance_id,
            TemplateAssignment.template_id,
        ).where(TemplateAssignment.id == assignment_id)
    ).one_or_none()
    if assignment_identity is None or (
        expected_template_id is not None and assignment_identity.template_id != expected_template_id
    ):
        msg = "Template synchronization assignment is missing"
        raise TemplateTargetSyncError(msg)

    instance = session.scalar(
        select(Instance).where(Instance.id == assignment_identity.instance_id).with_for_update()
    )
    template = session.scalar(
        select(Template).where(Template.id == assignment_identity.template_id).with_for_update()
    )
    assignment = session.scalar(
        select(TemplateAssignment).where(TemplateAssignment.id == assignment_id).with_for_update()
    )
    if (
        assignment is None
        or assignment.template_id != assignment_identity.template_id
        or assignment.instance_id != assignment_identity.instance_id
    ):
        msg = "Template synchronization assignment is missing"
        raise TemplateTargetSyncError(msg)
    if instance is None or template is None:
        msg = "Template synchronization target is missing"
        raise TemplateTargetSyncError(msg)
    if not _job_owns_sync_resource(job, template, assignment):
        raise _claim_lost()

    deployment = session.scalar(
        select(TemplateDeployment)
        .where(TemplateDeployment.assignment_id == assignment_id)
        .with_for_update()
    )
    return _OwnedTemplateTarget(
        instance=instance,
        template=template,
        assignment=assignment,
        deployment=deployment,
    )


def _job_matches_claim(job: JobExecution, claim: ExecutionClaim) -> bool:
    """Require the exact still-running attempt represented by the immutable claim."""

    return (
        job.task_name == claim.task_name
        and job.step == claim.step
        and job.attempt == claim.attempt
        and job.status is JobStatus.RUNNING
        and job.resource_type == claim.resource_type
        and job.resource_id == claim.resource_id
    )


def _job_owns_sync_resource(
    job: JobExecution,
    template: Template,
    assignment: TemplateAssignment,
) -> bool:
    """Mirror durable resource ownership without locking it before its parent rows."""

    if job.name == "template.sync" and job.resource_type == "template":
        return (
            job.resource_id == template.id
            and template.job_id == job.id
            and template.action == "syncing"
            and template.step == job.step
            and template.sync_status is TemplateSyncStatus.RUNNING
        )
    if job.name == "template_assignment.create" and job.resource_type == "template_assignment":
        return (
            job.resource_id == assignment.id
            and assignment.job_id == job.id
            and assignment.action == "creating"
            and assignment.step == job.step
            and assignment.status is TemplateAssignmentStatus.RUNNING
        )
    return False


def _claim_lost() -> TemplateTargetClaimLostError:
    """Build the stable sanitized error raised for fenced stale attempts."""

    return TemplateTargetClaimLostError("Template synchronization claim is no longer current")


def _managed_remote_template(
    client: CoderClient,
    organization_id: UUID,
    name: str,
    persisted_template_id: UUID | None,
    persisted_version_id: UUID | None,
) -> tuple[CoderTemplate | None, bool]:
    """Recover only a persisted template and reject unrelated same-name resources."""

    persisted = (
        client.template(persisted_template_id) if persisted_template_id is not None else None
    )
    named = client.template_by_name(organization_id, name)
    if persisted is not None:
        if named is not None and named.id != persisted.id:
            msg = "Remote template name is already owned outside CoderManager"
            raise TemplateTargetSyncError(msg)
        return persisted, named is not None
    if named is None:
        return None, False
    recovered = (persisted_template_id is not None and named.id == persisted_template_id) or (
        persisted_version_id is not None and named.active_version_id == persisted_version_id
    )
    if not recovered:
        msg = "Remote template name is already owned outside CoderManager"
        raise TemplateTargetSyncError(msg)
    return named, True


def sync_template_target(  # noqa: PLR0913
    snapshot: TemplateSourceSnapshot,
    archive: TemplateArchive,
    assignment_id: UUID,
    session_factory: sessionmaker[Session],
    *,
    claim: ExecutionClaim,
    heartbeat: Callable[[], None] | None = None,
) -> bool:
    """Synchronize one explicit assignment, returning whether remote work ran."""

    settings = get_settings()
    preparation = _prepare_deployment(
        claim,
        assignment_id,
        snapshot.id,
        archive.commit,
        snapshot.system_parameter_revision,
        session_factory,
        InstancePublicUrlConfig.from_settings(settings),
    )
    if preparation.already_applied:
        return False

    version_name = f"git-{archive.commit}-p{snapshot.system_parameter_revision}"
    try:
        with CoderClient(preparation.instance_url) as client:
            client.authenticate_prepared_admin(preparation.password)
            organization_id = client.default_organization_id()
            remote_template, found_by_name = _managed_remote_template(
                client,
                organization_id,
                snapshot.name,
                preparation.persisted_template_id,
                preparation.persisted_version_id,
            )
            _store_remote_ids(
                claim,
                assignment_id,
                organization_id=organization_id,
                coder_template_id=(remote_template.id if remote_template is not None else None),
                session_factory=session_factory,
            )

            remote_version = None
            if remote_template is not None and found_by_name:
                remote_version = client.template_version_by_name(
                    organization_id,
                    snapshot.name,
                    version_name,
                )
            if remote_version is None and preparation.persisted_version_id is not None:
                remote_version = client.template_version_for_recovery(
                    preparation.persisted_version_id
                )
            if remote_version is None:
                file_id = client.upload_template_archive(archive.content)
                remote_version = client.create_template_version(
                    organization_id,
                    file_id=file_id,
                    version_name=version_name,
                    template_id=(remote_template.id if remote_template is not None else None),
                    user_variable_values=preparation.system_values,
                )
                _store_remote_ids(
                    claim,
                    assignment_id,
                    coder_template_version_id=remote_version.id,
                    session_factory=session_factory,
                )

            if remote_version.status != "succeeded":
                remote_version = client.wait_template_version(
                    remote_version.id,
                    timeout_seconds=settings.template_sync_timeout_seconds,
                    poll_interval_seconds=settings.template_sync_poll_interval_seconds,
                    heartbeat=heartbeat,
                )
            if remote_version.archived:
                client.unarchive_template_version(remote_version.id)

            if remote_template is None:
                remote_template = client.create_template(
                    organization_id,
                    name=snapshot.name,
                    display_name=snapshot.display_name,
                    version_id=remote_version.id,
                )
                _store_remote_ids(
                    claim,
                    assignment_id,
                    coder_template_id=remote_template.id,
                    session_factory=session_factory,
                )
            else:
                client.activate_template_version(remote_template.id, remote_version.id)
    except TemplateTargetClaimLostError:
        raise
    except Exception:
        _finish_deployment(
            claim,
            assignment_id,
            archive.commit,
            snapshot.system_parameter_revision,
            success=False,
            session_factory=session_factory,
        )
        raise

    _finish_deployment(
        claim,
        assignment_id,
        archive.commit,
        snapshot.system_parameter_revision,
        success=True,
        session_factory=session_factory,
    )
    return True
