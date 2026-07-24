"""Public API of the Coder bootstrap domain."""

from coder_manager.domains.coder.client import CoderClient
from coder_manager.domains.coder.constants import (
    ADMIN_EMAIL,
    ADMIN_NAME,
    ADMIN_USERNAME,
)
from coder_manager.domains.coder.errors import (
    CoderFirstUserConflictError,
    CoderRequestError,
)
from coder_manager.domains.coder.models import CoderTemplate, CoderTemplateVersion
from coder_manager.domains.coder.service import bootstrap_admin_account

__all__ = [
    "ADMIN_EMAIL",
    "ADMIN_NAME",
    "ADMIN_USERNAME",
    "CoderClient",
    "CoderFirstUserConflictError",
    "CoderRequestError",
    "CoderTemplate",
    "CoderTemplateVersion",
    "bootstrap_admin_account",
]
