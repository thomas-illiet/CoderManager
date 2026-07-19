"""Public API of the Argo CD business domain."""

from coder_manager.domains.argocd.client import ArgoCdClient
from coder_manager.domains.argocd.config import ArgoCdConfig
from coder_manager.domains.argocd.errors import (
    ArgoCdApplicationNotFoundError,
    ArgoCdConfigurationError,
    ArgoCdRequestError,
)
from coder_manager.domains.argocd.models import ArgoCdApplicationStatus
from coder_manager.domains.argocd.service import (
    read_instance_application_status,
    reconcile_instance_application,
)

__all__ = [
    "ArgoCdApplicationNotFoundError",
    "ArgoCdApplicationStatus",
    "ArgoCdClient",
    "ArgoCdConfig",
    "ArgoCdConfigurationError",
    "ArgoCdRequestError",
    "read_instance_application_status",
    "reconcile_instance_application",
]
