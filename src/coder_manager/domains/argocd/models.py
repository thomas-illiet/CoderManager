"""Value objects returned by the Argo CD domain."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ArgoCdApplicationStatus:
    """Sanitized status fields returned for one Argo CD Application."""

    application_name: str
    sync_status: str | None
    health_status: str | None
    operation_phase: str | None
    revision: str | None
    reconciled_at: str | None
