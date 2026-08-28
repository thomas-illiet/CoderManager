"""Validated Argo CD connection and Application settings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from coder_manager.config import Environment
from coder_manager.constants import INSTANCE_SLUG_LENGTH
from coder_manager.domains.argocd.errors import ArgoCdConfigurationError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from coder_manager.config import Settings

APPLICATION_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
MAX_APPLICATION_NAME_LENGTH = 63
MAX_USERNAME_LENGTH = 255


@dataclass(frozen=True)
class CyberArkParameters:
    """CyberArk plugin parameters used by this deployment."""

    app_id: str
    cert_name: str
    key_name: str
    safe: str


@dataclass(frozen=True)
class ArgoCdClientConfig:
    """Validated settings required to access managed Argo CD Applications."""

    url: str
    token: str = field(repr=False)
    skip_ssl_verify: bool
    project: str
    application_prefix: str
    environment: Environment

    @classmethod
    def from_settings(cls, settings: Settings) -> ArgoCdClientConfig:
        """Validate the settings shared by read and mutation operations."""

        required: dict[str, str | None] = {
            "CODER_MANAGER_ENVIRONMENT": (
                settings.environment.value if settings.environment is not None else None
            ),
            "CODER_MANAGER_ARGOCD_URL": settings.argocd_url,
            "CODER_MANAGER_ARGOCD_TOKEN": (
                settings.argocd_token.get_secret_value()
                if settings.argocd_token is not None
                else None
            ),
            "CODER_MANAGER_ARGOCD_PROJECT_NAME": settings.argocd_project_name,
            "CODER_MANAGER_ARGOCD_APPLICATION_PREFIX": settings.argocd_application_prefix,
        }
        missing = [name for name, value in required.items() if not value or not value.strip()]
        if missing:
            joined = ", ".join(sorted(missing))
            msg = f"Missing required Argo CD client settings: {joined}"
            raise ArgoCdConfigurationError(msg)

        return cls(
            url=_required_value(required, "CODER_MANAGER_ARGOCD_URL").rstrip("/"),
            token=_required_value(required, "CODER_MANAGER_ARGOCD_TOKEN"),
            skip_ssl_verify=settings.argocd_skip_ssl_verify,
            project=_required_value(required, "CODER_MANAGER_ARGOCD_PROJECT_NAME"),
            application_prefix=_application_prefix(
                _required_value(required, "CODER_MANAGER_ARGOCD_APPLICATION_PREFIX"),
                "CODER_MANAGER_ARGOCD_APPLICATION_PREFIX",
            ),
            environment=Environment(_required_value(required, "CODER_MANAGER_ENVIRONMENT")),
        )


@dataclass(frozen=True)
class ArgoCdConfig(ArgoCdClientConfig):
    """Validated settings required to reconcile one Argo CD Application."""

    region: str
    repository_url: str
    repository_path: str
    target_revision: str
    destination_name: str
    cyberark: CyberArkParameters
    default_admins: tuple[str, ...]

    @classmethod
    def from_settings(cls, settings: Settings) -> ArgoCdConfig:
        """Validate runtime settings only when an Argo CD operation is requested."""

        client = ArgoCdClientConfig.from_settings(settings)
        required: dict[str, str | None] = {
            "CODER_MANAGER_ARGOCD_REGION": settings.argocd_region,
            "CODER_MANAGER_ARGOCD_REPOSITORY_URL": settings.argocd_repository_url,
            "CODER_MANAGER_ARGOCD_REPOSITORY_PATH": settings.argocd_repository_path,
            "CODER_MANAGER_ARGOCD_TARGET_REVISION": settings.argocd_target_revision,
            "CODER_MANAGER_ARGOCD_DESTINATION_NAME": settings.argocd_destination_name,
            "CODER_MANAGER_CYBERARK_APP_ID": settings.cyberark_app_id,
            "CODER_MANAGER_CYBERARK_CERT_NAME": settings.cyberark_cert_name,
            "CODER_MANAGER_CYBERARK_KEY_NAME": settings.cyberark_key_name,
            "CODER_MANAGER_CYBERARK_SAFE": settings.cyberark_safe,
        }
        missing = [name for name, value in required.items() if not value or not value.strip()]
        if missing:
            joined = ", ".join(sorted(missing))
            msg = f"Missing required Argo CD settings: {joined}"
            raise ArgoCdConfigurationError(msg)
        try:
            region = settings.require_argocd_region().upper()
        except ValueError as error:
            raise ArgoCdConfigurationError(str(error)) from error

        return cls(
            url=client.url,
            token=client.token,
            skip_ssl_verify=client.skip_ssl_verify,
            project=client.project,
            application_prefix=client.application_prefix,
            environment=client.environment,
            region=region,
            repository_url=_required_value(required, "CODER_MANAGER_ARGOCD_REPOSITORY_URL"),
            repository_path=_required_value(required, "CODER_MANAGER_ARGOCD_REPOSITORY_PATH"),
            target_revision=_required_value(required, "CODER_MANAGER_ARGOCD_TARGET_REVISION"),
            destination_name=_required_value(required, "CODER_MANAGER_ARGOCD_DESTINATION_NAME"),
            cyberark=CyberArkParameters(
                app_id=_required_value(required, "CODER_MANAGER_CYBERARK_APP_ID"),
                cert_name=_required_value(required, "CODER_MANAGER_CYBERARK_CERT_NAME"),
                key_name=_required_value(required, "CODER_MANAGER_CYBERARK_KEY_NAME"),
                safe=_required_value(required, "CODER_MANAGER_CYBERARK_SAFE"),
            ),
            default_admins=parse_default_admins(settings.default_admins),
        )


def _required_value(values: Mapping[str, str | None], name: str) -> str:
    """Return a stripped required setting after the caller's completeness check."""

    value = values[name]
    if value is None:  # pragma: no cover - checked by caller
        raise ArgoCdConfigurationError(name)
    return value.strip()


def _application_prefix(raw_value: str, setting_name: str) -> str:
    """Normalize and validate the Application name prefix."""

    prefix = raw_value.strip().lower()
    maximum_prefix_length = MAX_APPLICATION_NAME_LENGTH - INSTANCE_SLUG_LENGTH - 1
    if not APPLICATION_NAME_PATTERN.fullmatch(prefix) or len(prefix) > maximum_prefix_length:
        msg = f"{setting_name} is not a valid DNS label prefix"
        raise ArgoCdConfigurationError(msg)
    return prefix


def parse_default_admins(raw_value: str) -> tuple[str, ...]:
    """Normalize, validate, deduplicate, and sort default administrator names."""

    if not raw_value.strip():
        return ()
    raw_admins = raw_value.split(",")
    if any(not admin.strip() for admin in raw_admins):
        msg = "CODER_MANAGER_DEFAULT_ADMINS contains an empty username"
        raise ArgoCdConfigurationError(msg)
    admins = {admin.strip().lower() for admin in raw_admins}
    if any("\r" in admin or "\n" in admin for admin in admins):
        msg = "CODER_MANAGER_DEFAULT_ADMINS contains a username with line breaks"
        raise ArgoCdConfigurationError(msg)
    if any(len(admin) > MAX_USERNAME_LENGTH for admin in admins):
        msg = "CODER_MANAGER_DEFAULT_ADMINS contains a username longer than 255 characters"
        raise ArgoCdConfigurationError(msg)
    return tuple(sorted(admins))
