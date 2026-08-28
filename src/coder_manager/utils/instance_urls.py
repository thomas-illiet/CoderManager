"""Runtime construction of public Coder instance URLs."""

from dataclasses import dataclass
from typing import Self

from coder_manager.config import Settings


@dataclass(frozen=True, slots=True)
class InstancePublicUrlConfig:
    """Validated runtime configuration used to build public instance URLs."""

    instance_base_domain: str

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build public URL configuration from validated application settings."""

        return cls(
            instance_base_domain=settings.require_instance_base_domain(),
        )

    def url_for(self, slug: str) -> str:
        """Return the public HTTPS URL for one instance identity."""

        return f"https://{slug}.{self.instance_base_domain}"
