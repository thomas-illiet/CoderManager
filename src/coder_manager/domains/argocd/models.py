"""Value objects used by the Argo CD domain."""

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import SecretBytes, SecretStr


class ArgoCdMutationStatus(StrEnum):
    """Outcome of one requested Argo CD mutation."""

    COMPLETED = "completed"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class ArgoCdReconcileResult:
    """Typed result of one Application reconciliation request."""

    status: ArgoCdMutationStatus
    application_name: str


@dataclass(frozen=True, slots=True)
class InstanceHelmValues:
    """Instance-specific public endpoint and managed database Helm values."""

    slug: str
    environment: str
    public_url: str
    database_username: str
    database_password: SecretStr
    database_host: str
    database_name: str
    managed_database_name: str
    database_schema: str
    kubeconfig: SecretBytes | None = None

    @property
    def base_domain(self) -> str:
        """Return the hostname of the public instance URL without its scheme."""

        hostname = urlsplit(self.public_url).hostname
        if hostname is None:  # pragma: no cover - validated URL configuration invariant
            msg = "Instance public URL does not contain a hostname"
            raise ValueError(msg)
        return hostname

    @property
    def wildcard_access_host(self) -> str:
        """Return the wildcard hostname associated with the public instance URL."""

        return f"*.{self.base_domain}"


@dataclass(frozen=True)
class ArgoCdApplicationStatus:
    """Sanitized status fields returned for one Argo CD Application."""

    application_name: str
    sync_status: str | None
    health_status: str | None
    operation_phase: str | None
    revision: str | None
    reconciled_at: str | None
