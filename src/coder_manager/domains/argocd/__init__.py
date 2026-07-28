"""Public API of the Argo CD business domain."""

from coder_manager.domains.argocd.client import ArgoCdClient
from coder_manager.domains.argocd.config import ArgoCdConfig, parse_default_admins
from coder_manager.domains.argocd.errors import (
    ArgoCdApplicationNotFoundError,
    ArgoCdConfigurationError,
    ArgoCdRequestError,
)
from coder_manager.domains.argocd.models import ArgoCdApplicationStatus, InstanceHelmValues
from coder_manager.domains.argocd.service import (
    delete_instance_application,
    instance_application_exists,
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
    "InstanceHelmValues",
    "delete_instance_application",
    "instance_application_exists",
    "parse_default_admins",
    "read_instance_application_status",
    "reconcile_instance_application",
]
