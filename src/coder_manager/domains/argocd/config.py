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
INSTANCE_ENVIRONMENTS = ("development", "staging", "production")
CYBERARK_FIELDS = ("app_id", "cert_name", "key_name", "safe")


@dataclass(frozen=True)
class CyberArkParameters:
    """Plugin parameters selected for one instance environment."""

    app_id: str
    cert_name: str
    key_name: str
    safe: str


@dataclass(frozen=True)
class ArgoCdClientConfig:
    """Validated settings required to access one Argo CD Application."""

    url: str
    tokens: Mapping[str, str] = field(repr=False)
    skip_ssl_verify: bool
    projects: Mapping[str, str]
    application_prefixes: Mapping[str, str]

    @classmethod
    def from_settings(cls, settings: Settings) -> ArgoCdClientConfig:
        """Validate the settings shared by read and mutation operations."""

        required: dict[str, str | None] = {"CODER_MANAGER_ARGOCD_URL": settings.argocd_url}
        required.update(_token_settings(settings))
        required.update(_project_settings(settings))
        required.update(_application_prefix_settings(settings))
        missing = [name for name, value in required.items() if not value or not value.strip()]
        if missing:
            joined = ", ".join(sorted(missing))
            msg = f"Missing required Argo CD client settings: {joined}"
            raise ArgoCdConfigurationError(msg)

        return cls(
            url=_required_value(required, "CODER_MANAGER_ARGOCD_URL").rstrip("/"),
            tokens=_tokens(required),
            skip_ssl_verify=settings.argocd_skip_ssl_verify,
            projects=_projects(required),
            application_prefixes=_application_prefixes(required),
        )

    def token_for(self, environment: str) -> str:
        """Return the Argo CD bearer token configured for one environment."""

        try:
            return self.tokens[environment]
        except KeyError as error:  # pragma: no cover - callers use domain enum values
            msg = f"Unsupported Argo CD token environment: {environment}"
            raise ArgoCdConfigurationError(msg) from error

    def project_for(self, environment: str) -> str:
        """Return the Argo CD project configured for one environment."""

        try:
            return self.projects[environment]
        except KeyError as error:  # pragma: no cover - callers use domain enum values
            msg = f"Unsupported Argo CD project: {environment}"
            raise ArgoCdConfigurationError(msg) from error

    def application_prefix_for(self, environment: str) -> str:
        """Return the Application name prefix configured for one environment."""

        try:
            return self.application_prefixes[environment]
        except KeyError as error:  # pragma: no cover - callers use domain enum values
            msg = f"Unsupported Argo CD Application prefix: {environment}"
            raise ArgoCdConfigurationError(msg) from error


@dataclass(frozen=True)
class ArgoCdConfig(ArgoCdClientConfig):
    """Validated settings required to reconcile one Argo CD Application."""

    region: str
    repository_url: str
    repository_path: str
    target_revision: str
    destination_names: Mapping[str, str]
    cyberark_parameters: Mapping[str, CyberArkParameters]
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
        }
        required.update(_destination_settings(settings))
        required.update(_cyberark_settings(settings))
        missing = [name for name, value in required.items() if not value or not value.strip()]
        if missing:
            joined = ", ".join(sorted(missing))
            msg = f"Missing required Argo CD settings: {joined}"
            raise ArgoCdConfigurationError(msg)

        return cls(
            url=client.url,
            tokens=client.tokens,
            skip_ssl_verify=client.skip_ssl_verify,
            projects=client.projects,
            application_prefixes=client.application_prefixes,
            region=_required_value(required, "CODER_MANAGER_ARGOCD_REGION").upper(),
            repository_url=_required_value(required, "CODER_MANAGER_ARGOCD_REPOSITORY_URL"),
            repository_path=_required_value(required, "CODER_MANAGER_ARGOCD_REPOSITORY_PATH"),
            target_revision=_required_value(required, "CODER_MANAGER_ARGOCD_TARGET_REVISION"),
            destination_names=_destination_names(required),
            cyberark_parameters=_cyberark_parameters(required),
            default_admins=parse_default_admins(settings.default_admins),
        )

    def destination_for(self, environment: str) -> str:
        """Return the Argo CD destination configured for one environment."""

        try:
            return self.destination_names[environment]
        except KeyError as error:  # pragma: no cover - callers use domain enum values
            msg = f"Unsupported Argo CD destination: {environment}"
            raise ArgoCdConfigurationError(msg) from error

    def cyberark_for(self, environment: str) -> CyberArkParameters:
        """Return the CyberArk parameters configured for one instance target."""

        try:
            return self.cyberark_parameters[environment]
        except KeyError as error:  # pragma: no cover - callers use domain enum values
            msg = f"Unsupported CyberArk target: {environment}"
            raise ArgoCdConfigurationError(msg) from error


def _required_value(values: Mapping[str, str | None], name: str) -> str:
    """Return a stripped required setting after the caller's completeness check."""

    value = values[name]
    if value is None:  # pragma: no cover - checked by caller
        raise ArgoCdConfigurationError(name)
    return value.strip()


def _application_prefix(raw_value: str, setting_name: str) -> str:
    """Normalize and validate the Application name prefix."""

    prefix = raw_value.strip().lower()
    maximum_prefix_length = MAX_APPLICATION_NAME_LENGTH - UUID_HEX_LENGTH - 1
    if not APPLICATION_NAME_PATTERN.fullmatch(prefix) or len(prefix) > maximum_prefix_length:
        msg = f"{setting_name} is not a valid DNS label prefix"
        raise ArgoCdConfigurationError(msg)
    return prefix


def _token_settings(settings: Settings) -> dict[str, str | None]:
    """Collect the three environment-specific Argo CD bearer tokens."""

    return {
        _token_environment_name(environment): (
            token.get_secret_value()
            if (token := getattr(settings, f"argocd_{environment}_token")) is not None
            else None
        )
        for environment in INSTANCE_ENVIRONMENTS
    }


def _tokens(values: Mapping[str, str | None]) -> Mapping[str, str]:
    """Build an immutable token lookup for every environment."""

    return MappingProxyType(
        {
            environment: _required_value(values, _token_environment_name(environment))
            for environment in INSTANCE_ENVIRONMENTS
        }
    )


def _token_environment_name(environment: str) -> str:
    """Return the public bearer-token environment variable name."""

    return f"CODER_MANAGER_ARGOCD_{environment}_TOKEN".upper()


def _application_prefix_settings(settings: Settings) -> dict[str, str | None]:
    """Collect the three environment-specific Application name prefixes."""

    return {
        _application_prefix_environment_name(environment): getattr(
            settings,
            f"argocd_{environment}_application_prefix",
        )
        for environment in INSTANCE_ENVIRONMENTS
    }


def _application_prefixes(values: Mapping[str, str | None]) -> Mapping[str, str]:
    """Build an immutable validated prefix lookup for every environment."""

    return MappingProxyType(
        {
            environment: _application_prefix(
                _required_value(values, _application_prefix_environment_name(environment)),
                _application_prefix_environment_name(environment),
            )
            for environment in INSTANCE_ENVIRONMENTS
        }
    )


def _application_prefix_environment_name(environment: str) -> str:
    """Return the public Application-prefix environment variable name."""

    return f"CODER_MANAGER_ARGOCD_{environment}_APPLICATION_PREFIX".upper()


def _project_settings(settings: Settings) -> dict[str, str | None]:
    """Collect the three environment-specific Argo CD projects."""

    return {
        _project_environment_name(environment): getattr(
            settings,
            f"argocd_{environment}_project_name",
        )
        for environment in INSTANCE_ENVIRONMENTS
    }


def _projects(values: Mapping[str, str | None]) -> Mapping[str, str]:
    """Build an immutable project lookup for every environment."""

    return MappingProxyType(
        {
            environment: _required_value(
                values,
                _project_environment_name(environment),
            )
            for environment in INSTANCE_ENVIRONMENTS
        }
    )


def _project_environment_name(environment: str) -> str:
    """Return the public project environment variable name."""

    return f"CODER_MANAGER_ARGOCD_{environment}_PROJECT_NAME".upper()


def _destination_settings(settings: Settings) -> dict[str, str | None]:
    """Collect the three environment-specific Argo CD destinations."""

    return {
        _destination_environment_name(environment): getattr(
            settings,
            f"argocd_{environment}_destination_name",
        )
        for environment in INSTANCE_ENVIRONMENTS
    }


def _destination_names(values: Mapping[str, str | None]) -> Mapping[str, str]:
    """Build an immutable destination lookup for every environment."""

    return MappingProxyType(
        {
            environment: _required_value(
                values,
                _destination_environment_name(environment),
            )
            for environment in INSTANCE_ENVIRONMENTS
        }
    )


def _destination_environment_name(environment: str) -> str:
    """Return the public destination environment variable name."""

    return f"CODER_MANAGER_ARGOCD_{environment}_DESTINATION_NAME".upper()


def _cyberark_settings(settings: Settings) -> dict[str, str | None]:
    """Collect the three environment-specific CyberArk setting groups."""

    return {
        _cyberark_environment_name(environment, field_name): getattr(
            settings,
            f"cyberark_{environment}_{field_name}",
        )
        for environment in INSTANCE_ENVIRONMENTS
        for field_name in CYBERARK_FIELDS
    }


def _cyberark_parameters(
    values: Mapping[str, str | None],
) -> Mapping[str, CyberArkParameters]:
    """Build an immutable lookup for all supported instance targets."""

    parameters = {
        environment: CyberArkParameters(
            app_id=_required_value(
                values,
                _cyberark_environment_name(environment, "app_id"),
            ),
            cert_name=_required_value(
                values,
                _cyberark_environment_name(environment, "cert_name"),
            ),
            key_name=_required_value(
                values,
                _cyberark_environment_name(environment, "key_name"),
            ),
            safe=_required_value(
                values,
                _cyberark_environment_name(environment, "safe"),
            ),
        )
        for environment in INSTANCE_ENVIRONMENTS
    }
    return MappingProxyType(parameters)


def _cyberark_environment_name(environment: str, field_name: str) -> str:
    """Return the public environment variable name for one CyberArk value."""

    return f"CODER_MANAGER_CYBERARK_{environment}_{field_name}".upper()


def parse_default_admins(raw_value: str) -> tuple[str, ...]:
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
