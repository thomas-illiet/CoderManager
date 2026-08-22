"""Runtime construction of public Coder instance URLs."""

from dataclasses import dataclass
from typing import Self

from coder_manager.config import Settings
from coder_manager.models import InstanceEnvironment

ENVIRONMENT_DNS_LABELS = {
    InstanceEnvironment.DEVELOPMENT: "dev",
    InstanceEnvironment.STAGING: "staging",
    InstanceEnvironment.PRODUCTION: "cib",
}


@dataclass(frozen=True, slots=True)
class InstancePublicUrlConfig:
    """Validated runtime configuration used to build public instance URLs."""

    region: str
    instance_domain: str

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build public URL configuration from validated application settings."""

        return cls(
            region=settings.require_instance_region(),
            instance_domain=settings.instance_domain,
        )

    def url_for(self, slug: str, environment: InstanceEnvironment) -> str:
        """Return the public HTTPS URL for one instance identity."""

        environment_label = ENVIRONMENT_DNS_LABELS[environment]
        return f"https://{slug}.{self.region}.{self.instance_domain}.{environment_label}.echonet"
