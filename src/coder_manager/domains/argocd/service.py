"""Application services exposed by the Argo CD domain."""

from __future__ import annotations

from typing import TYPE_CHECKING

from coder_manager.config import Settings, get_settings
from coder_manager.domains.argocd.client import ArgoCdClient
from coder_manager.domains.argocd.config import ArgoCdClientConfig, ArgoCdConfig

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID

    from coder_manager.domains.argocd.models import (
        ArgoCdApplicationStatus,
        ArgoCdMutationStatus,
        ArgoCdReconcileResult,
        InstanceHelmValues,
    )


def reconcile_instance_application(
    instance_id: UUID,
    slug: str,
    attached_name: str | None,
    members: Iterable[tuple[str, str]],
    helm_values: InstanceHelmValues,
) -> ArgoCdReconcileResult:
    """Reconcile one instance using the process-wide Argo CD configuration."""

    config = ArgoCdConfig.from_settings(get_settings())
    with ArgoCdClient(config) as client:
        return client.ensure_application(
            instance_id,
            slug,
            attached_name,
            members,
            helm_values,
        )


def delete_instance_application(
    slug: str,
    attached_name: str | None,
    environment: str,
) -> ArgoCdMutationStatus:
    """Delete one instance's Application using the process-wide configuration."""

    config = ArgoCdConfig.from_settings(get_settings())
    with ArgoCdClient(config) as client:
        return client.delete_application(slug, attached_name, environment)


def instance_application_exists(
    slug: str,
    attached_name: str | None,
    environment: str,
    settings: Settings | None = None,
) -> bool:
    """Return whether one strict instance Application currently exists."""

    config = ArgoCdConfig.from_settings(settings or get_settings())
    with ArgoCdClient(config) as client:
        return client.application_exists(slug, attached_name, environment)


def read_instance_application_status(
    slug: str,
    attached_name: str | None,
    environment: str,
    settings: Settings,
) -> ArgoCdApplicationStatus:
    """Read one instance's remote Argo CD status with explicit API settings."""

    config = ArgoCdClientConfig.from_settings(settings)
    with ArgoCdClient(config) as client:
        return client.get_application_status(slug, attached_name, environment)
