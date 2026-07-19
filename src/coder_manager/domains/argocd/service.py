"""Application services exposed by the Argo CD domain."""

from __future__ import annotations

from typing import TYPE_CHECKING

from coder_manager.config import Settings, get_settings
from coder_manager.domains.argocd.client import ArgoCdClient
from coder_manager.domains.argocd.config import ArgoCdConfig

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID

    from coder_manager.domains.argocd.models import ArgoCdApplicationStatus


def reconcile_instance_application(
    instance_id: UUID,
    attached_name: str | None,
    members: Iterable[tuple[str, str]],
) -> str:
    """Reconcile one instance using the process-wide Argo CD configuration."""

    config = ArgoCdConfig.from_settings(get_settings())
    with ArgoCdClient(config) as client:
        return client.ensure_application(instance_id, attached_name, members)


def read_instance_application_status(
    instance_id: UUID,
    attached_name: str | None,
    settings: Settings,
) -> ArgoCdApplicationStatus:
    """Read one instance's remote Argo CD status with explicit API settings."""

    config = ArgoCdConfig.from_settings(settings)
    with ArgoCdClient(config) as client:
        return client.get_application_status(instance_id, attached_name)
