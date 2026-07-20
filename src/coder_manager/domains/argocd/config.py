"""Validated Argo CD connection and Application settings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from coder_manager.domains.argocd.errors import ArgoCdConfigurationError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from coder_manager.config import Settings

APPLICATION_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
MAX_APPLICATION_NAME_LENGTH = 63
UUID_HEX_LENGTH = 32
MAX_USERNAME_LENGTH = 255
INSTANCE_REGIONS = ("emea", "apac", "amer")
INSTANCE_ENVIRONMENTS = ("development", "staging", "production")
CYBERARK_FIELDS = ("app_id", "cert_name", "key_name", "safe")


@dataclass(frozen=True)
class CyberArkParameters:
    """Plugin parameters selected for one instance region and environment."""

    app_id: str
    cert_name: str
    key_name: str
    safe: str


@dataclass(frozen=True)
class ArgoCdConfig:
    """Validated settings required to reconcile one Argo CD Application."""

    url: str
    token: str = field(repr=False)
    skip_ssl_verify: bool
    project: str
    application_prefix: str
    repository_url: str
    repository_path: str
    target_revision: str
    destination_name: str
    cyberark_parameters: Mapping[tuple[str, str], CyberArkParameters]
    default_admins: tuple[str, ...]

    @classmethod
    def from_settings(cls, settings: Settings) -> ArgoCdConfig:
        """Validate runtime settings only when an Argo CD operation is requested."""

        required: dict[str, str | None] = {
            "CODER_MANAGER_ARGOCD_URL": settings.argocd_url,
            "CODER_MANAGER_ARGOCD_TOKEN": (
                settings.argocd_token.get_secret_value() if settings.argocd_token else None
            ),
            "CODER_MANAGER_ARGOCD_PROJECT": settings.argocd_project,
            "CODER_MANAGER_ARGOCD_REPOSITORY_URL": settings.argocd_repository_url,
            "CODER_MANAGER_ARGOCD_REPOSITORY_PATH": settings.argocd_repository_path,
            "CODER_MANAGER_ARGOCD_TARGET_REVISION": settings.argocd_target_revision,
            "CODER_MANAGER_ARGOCD_DESTINATION_NAME": settings.argocd_destination_name,
        }
        required.update(_cyberark_settings(settings))
        missing = [name for name, value in required.items() if not value or not value.strip()]
        if missing:
            joined = ", ".join(sorted(missing))
            msg = f"Missing required Argo CD settings: {joined}"
            raise ArgoCdConfigurationError(msg)

        prefix = settings.argocd_application_prefix.strip().lower()
        maximum_prefix_length = MAX_APPLICATION_NAME_LENGTH - UUID_HEX_LENGTH - 1
        if not APPLICATION_NAME_PATTERN.fullmatch(prefix) or len(prefix) > maximum_prefix_length:
            msg = "CODER_MANAGER_ARGOCD_APPLICATION_PREFIX is not a valid DNS label prefix"
            raise ArgoCdConfigurationError(msg)

        return cls(
            url=_required_value(required, "CODER_MANAGER_ARGOCD_URL").rstrip("/"),
            token=_required_value(required, "CODER_MANAGER_ARGOCD_TOKEN"),
            skip_ssl_verify=settings.argocd_skip_ssl_verify,
            project=_required_value(required, "CODER_MANAGER_ARGOCD_PROJECT"),
            application_prefix=prefix,
            repository_url=_required_value(required, "CODER_MANAGER_ARGOCD_REPOSITORY_URL"),
            repository_path=_required_value(required, "CODER_MANAGER_ARGOCD_REPOSITORY_PATH"),
            target_revision=_required_value(required, "CODER_MANAGER_ARGOCD_TARGET_REVISION"),
            destination_name=_required_value(required, "CODER_MANAGER_ARGOCD_DESTINATION_NAME"),
            cyberark_parameters=_cyberark_parameters(required),
            default_admins=_parse_default_admins(settings.default_admins),
        )

    def cyberark_for(self, region: str, environment: str) -> CyberArkParameters:
        """Return the CyberArk parameters configured for one instance target."""

        try:
            return self.cyberark_parameters[(region, environment)]
        except KeyError as error:  # pragma: no cover - callers use domain enum values
            msg = f"Unsupported CyberArk target: {region}/{environment}"
            raise ArgoCdConfigurationError(msg) from error


def _required_value(values: Mapping[str, str | None], name: str) -> str:
    """Return a stripped required setting after the caller's completeness check."""

    value = values[name]
    if value is None:  # pragma: no cover - checked by caller
        raise ArgoCdConfigurationError(name)
    return value.strip()


def _cyberark_settings(settings: Settings) -> dict[str, str | None]:
    """Collect the nine region/environment CyberArk setting groups."""

    return {
        _cyberark_environment_name(region, environment, field_name): getattr(
            settings,
            f"cyberark_{region}_{environment}_{field_name}",
        )
        for region in INSTANCE_REGIONS
        for environment in INSTANCE_ENVIRONMENTS
        for field_name in CYBERARK_FIELDS
    }


def _cyberark_parameters(
    values: Mapping[str, str | None],
) -> Mapping[tuple[str, str], CyberArkParameters]:
    """Build an immutable lookup for all supported instance targets."""

    parameters = {
        (region, environment): CyberArkParameters(
            app_id=_required_value(
                values,
                _cyberark_environment_name(region, environment, "app_id"),
            ),
            cert_name=_required_value(
                values,
                _cyberark_environment_name(region, environment, "cert_name"),
            ),
            key_name=_required_value(
                values,
                _cyberark_environment_name(region, environment, "key_name"),
            ),
            safe=_required_value(
                values,
                _cyberark_environment_name(region, environment, "safe"),
            ),
        )
        for region in INSTANCE_REGIONS
        for environment in INSTANCE_ENVIRONMENTS
    }
    return MappingProxyType(parameters)


def _cyberark_environment_name(region: str, environment: str, field_name: str) -> str:
    """Return the public environment variable name for one CyberArk value."""

    return f"CODER_MANAGER_CYBERARK_{region}_{environment}_{field_name}".upper()


def _parse_default_admins(raw_value: str) -> tuple[str, ...]:
    """Normalize, validate, deduplicate, and sort default administrator names."""

    if not raw_value.strip():
        return ()
    raw_admins = raw_value.split(",")
    if any(not admin.strip() for admin in raw_admins):
        msg = "CODER_MANAGER_DEFAULT_ADMINS contains an empty username"
        raise ArgoCdConfigurationError(msg)
    admins = {admin.strip().lower() for admin in raw_admins}
    if any(len(admin) > MAX_USERNAME_LENGTH for admin in admins):
        msg = "CODER_MANAGER_DEFAULT_ADMINS contains a username longer than 255 characters"
        raise ArgoCdConfigurationError(msg)
    return tuple(sorted(admins))
