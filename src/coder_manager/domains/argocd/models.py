"""Value objects used by the Argo CD domain."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import SecretStr


@dataclass(frozen=True, slots=True)
class InstanceHelmValues:
    """Instance-specific public endpoint and managed database Helm values."""

    region: str
    environment: str
    public_url: str
    database_username: str
    database_password: SecretStr
    database_host: str
    database_name: str
    database_schema: str

    @property
    def wildcard_access_host(self) -> str:
        """Return the wildcard hostname associated with the public instance URL."""

        hostname = urlsplit(self.public_url).hostname
        if hostname is None:  # pragma: no cover - persisted instance URL invariant
            msg = "Instance public URL does not contain a hostname"
            raise ValueError(msg)
        return f"*.{hostname}"


@dataclass(frozen=True)
class ArgoCdApplicationStatus:
    """Sanitized status fields returned for one Argo CD Application."""

    application_name: str
    sync_status: str | None
    health_status: str | None
    operation_phase: str | None
    revision: str | None
    reconciled_at: str | None
