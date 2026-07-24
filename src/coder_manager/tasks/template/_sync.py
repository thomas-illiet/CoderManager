"""Shared synchronous helpers for publishing one template to Coder instances."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import SecretStr
from sqlalchemy import or_, select

from coder_manager.config import get_settings
from coder_manager.crypto import InstancePasswordCipher
from coder_manager.domains.coder import CoderClient
from coder_manager.domains.template_source import TemplateArchive, fetch_branch_archive
from coder_manager.models import (
    Instance,
    InstanceStatus,
    Template,
    TemplateDeployment,
    TemplateDeploymentStatus,
    TemplateScope,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TemplateSourceSnapshot:
    """Source and display fields read atomically for one synchronization."""

    id: UUID
    display_name: str
    name: str
    git_url: str
    source_path: str
    branch: str


class TemplateTargetSyncError(Exception):
    """Raised after a sanitized target synchronization failure."""


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
        )


def fetch_template_archive(snapshot: TemplateSourceSnapshot) -> TemplateArchive:
    """Fetch the configured branch using the process allowlist."""

    return fetch_branch_archive(
        snapshot.git_url,
        snapshot.branch,
        snapshot.source_path,
    )


def ready_instance_ids(
    template_id: UUID,
    session_factory: sessionmaker[Session],
) -> tuple[UUID, ...]:
    """Return ready instances compatible with one template scope."""

    with session_factory() as session:
        template = session.get(Template, template_id)
        if template is None:
            msg = "Template is missing"
            raise TemplateTargetSyncError(msg)
        statement = select(Instance.id).where(
            Instance.status == InstanceStatus.SUCCESS,
            Instance.action != "deleting",
        )
        if template.scope is TemplateScope.APPLICATION:
            statement = statement.where(Instance.application == template.application)
        return tuple(session.scalars(statement.order_by(Instance.id)))


def compatible_template_ids(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
) -> tuple[UUID, ...]:
    """Return global and application templates required by one instance."""

    with session_factory() as session:
        instance = session.get(Instance, instance_id)
        if instance is None:
            msg = "Instance is missing"
            raise TemplateTargetSyncError(msg)
        return tuple(
            session.scalars(
                select(Template.id)
                .where(
                    or_(
                        Template.scope == TemplateScope.GLOBAL,
                        (
                            (Template.scope == TemplateScope.APPLICATION)
                            & (Template.application == instance.application)
                        ),
                    )
                )
                .order_by(Template.id)
            )
        )


def _prepare_deployment(
    template_id: UUID,
    instance_id: UUID,
    commit: str,
    session_factory: sessionmaker[Session],
) -> tuple[bool, str, SecretStr, UUID | None]:
    """Mark one target running and return its current remote version if reusable."""

    with session_factory() as session:
        instance = session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        template = session.get(Template, template_id)
        if instance is None or template is None:
            msg = "Template synchronization target is missing"
            raise TemplateTargetSyncError(msg)
        if instance.password_enc is None:
            msg = "Coder administrator password is not initialized"
            raise TemplateTargetSyncError(msg)

        deployment = session.scalar(
            select(TemplateDeployment)
            .where(
                TemplateDeployment.template_id == template_id,
                TemplateDeployment.instance_id == instance_id,
            )
            .with_for_update()
        )
        if deployment is None:
            deployment = TemplateDeployment(
                template_id=template_id,
                instance_id=instance_id,
            )
            session.add(deployment)
            session.flush()
        if (
            deployment.status is TemplateDeploymentStatus.SUCCESS
            and deployment.applied_commit == commit
        ):
            return True, instance.instance_url, SecretStr(""), None

        if deployment.target_commit != commit:
            deployment.coder_template_version_id = None
        deployment.target_commit = commit
        deployment.status = TemplateDeploymentStatus.RUNNING
        reusable_version_id = deployment.coder_template_version_id
        password = InstancePasswordCipher(get_settings().crypto_key).decrypt(
            instance.password_enc,
            instance.id,
        )
        session.commit()
        return False, instance.instance_url, password, reusable_version_id


def _store_remote_ids(  # noqa: PLR0913
    template_id: UUID,
    instance_id: UUID,
    *,
    organization_id: UUID | None = None,
    coder_template_id: UUID | None = None,
    coder_template_version_id: UUID | None = None,
    session_factory: sessionmaker[Session],
) -> None:
    """Persist remote identifiers immediately to close retry windows."""

    with session_factory() as session:
        deployment = session.scalar(
            select(TemplateDeployment)
            .where(
                TemplateDeployment.template_id == template_id,
                TemplateDeployment.instance_id == instance_id,
            )
            .with_for_update()
        )
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


def _finish_deployment(
    template_id: UUID,
    instance_id: UUID,
    commit: str,
    *,
    success: bool,
    session_factory: sessionmaker[Session],
) -> None:
    """Store only the current success or error state for one target."""

    with session_factory() as session:
        deployment = session.scalar(
            select(TemplateDeployment)
            .where(
                TemplateDeployment.template_id == template_id,
                TemplateDeployment.instance_id == instance_id,
            )
            .with_for_update()
        )
        if deployment is None:
            return
        deployment.status = (
            TemplateDeploymentStatus.SUCCESS if success else TemplateDeploymentStatus.ERROR
        )
        if success:
            deployment.applied_commit = commit
        session.commit()


def sync_template_target(
    snapshot: TemplateSourceSnapshot,
    archive: TemplateArchive,
    instance_id: UUID,
    session_factory: sessionmaker[Session],
    *,
    heartbeat: Callable[[], None] | None = None,
) -> bool:
    """Synchronize one branch HEAD to one instance, returning whether work ran."""

    already_applied, instance_url, password, reusable_version_id = _prepare_deployment(
        snapshot.id,
        instance_id,
        archive.commit,
        session_factory,
    )
    if already_applied:
        return False

    settings = get_settings()
    version_name = f"git-{archive.commit}"
    try:
        with CoderClient(instance_url) as client:
            client.authenticate_prepared_admin(password)
            organization_id = client.default_organization_id()
            remote_template = client.template_by_name(
                organization_id,
                snapshot.name,
            )
            coder_template_id = remote_template.id if remote_template is not None else None
            _store_remote_ids(
                snapshot.id,
                instance_id,
                organization_id=organization_id,
                coder_template_id=coder_template_id,
                session_factory=session_factory,
            )

            remote_version = None
            if remote_template is not None:
                remote_version = client.template_version_by_name(
                    organization_id,
                    snapshot.name,
                    version_name,
                )
            if remote_version is None and reusable_version_id is not None:
                remote_version = client.template_version(reusable_version_id)
            if remote_version is None:
                file_id = client.upload_template_archive(archive.content)
                remote_version = client.create_template_version(
                    organization_id,
                    file_id=file_id,
                    version_name=version_name,
                    template_id=coder_template_id,
                )
                _store_remote_ids(
                    snapshot.id,
                    instance_id,
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
                    snapshot.id,
                    instance_id,
                    coder_template_id=remote_template.id,
                    session_factory=session_factory,
                )
            else:
                client.activate_template_version(remote_template.id, remote_version.id)
    except Exception:
        _finish_deployment(
            snapshot.id,
            instance_id,
            archive.commit,
            success=False,
            session_factory=session_factory,
        )
        raise

    _finish_deployment(
        snapshot.id,
        instance_id,
        archive.commit,
        success=True,
        session_factory=session_factory,
    )
    return True
