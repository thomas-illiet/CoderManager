"""Argo CD Application naming, payload and status transformations."""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from coder_manager.domains.argocd.errors import ArgoCdRequestError
from coder_manager.domains.argocd.models import (
    ArgoCdApplicationStatus,
    InstanceHelmValues,
)
from coder_manager.domains.coder import ADMIN_USERNAME

if TYPE_CHECKING:
    from uuid import UUID

    from coder_manager.domains.argocd.config import ArgoCdConfig

MANAGED_LABEL = "coder-manager/managed"
INSTANCE_ID_LABEL = "coder-manager/instance-id"
APPLICATION_NAMESPACE = "app-coder-system"
ENVIRONMENT_VALUE_FILES = {
    "development": "values-dev.yaml",
    "staging": "values-stg.yaml",
    "production": "values-prd.yaml",
}
ACTIVE_OPERATION_PHASES = frozenset({"Running", "Terminating"})


def application_name(
    config: ArgoCdConfig,
    slug: str,
    attached_name: str | None,
) -> str:
    """Return the attached or strict slug-based Application name."""

    if attached_name:
        return attached_name
    return f"{config.application_prefix}-{slug}"


def application_payload(
    config: ArgoCdConfig,
    name: str,
    instance_id: UUID,
    members: Iterable[tuple[str, str]],
    helm_values: InstanceHelmValues,
) -> dict[str, Any]:
    """Build the desired Argo CD Application for one managed instance.

    The payload maps active members into deterministic plugin Helm arguments,
    supplies the CyberArk lookup parameters, and pins the destination namespace
    to the shared Coder application namespace.
    """

    users, admins = _member_values(config.default_admins, members)
    cyberark = config.cyberark_for(helm_values.environment)
    identifier = helm_values.slug
    helm_argument_lines = [
        f"--values {ENVIRONMENT_VALUE_FILES[helm_values.environment]}",
        f"--namespace {APPLICATION_NAMESPACE}",
        _helm_scalar_argument(
            "policy.config.allowedUsernames",
            ",".join(users),
        ),
        _helm_scalar_argument(
            "policy.config.adminUsernames",
            ",".join(admins),
        ),
        _helm_scalar_argument("global.baseDomain", helm_values.base_domain),
        _helm_scalar_argument("global.identifier", identifier),
        _helm_scalar_argument(
            "server.config.postgres.username",
            f"<secret:{helm_values.managed_database_name}#username>",
        ),
        _helm_scalar_argument(
            "server.config.postgres.password",
            f"<secret:{helm_values.managed_database_name}#password>",
        ),
        _helm_scalar_argument(
            "server.config.postgres.host",
            helm_values.database_host,
        ),
        _helm_scalar_argument(
            "server.config.postgres.database",
            helm_values.database_name,
        ),
        _helm_scalar_argument(
            "server.config.postgres.schema",
            helm_values.database_schema,
        ),
    ]
    if helm_values.kubeconfig is not None:
        kubeconfig_base64 = b64encode(helm_values.kubeconfig.get_secret_value()).decode("ascii")
        helm_argument_lines.append(_helm_scalar_argument("server.config.kube", kubeconfig_base64))
    helm_arguments = "\n".join(helm_argument_lines)
    helm_arguments += "\n"
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": name,
            "labels": {
                MANAGED_LABEL: "true",
                INSTANCE_ID_LABEL: str(instance_id),
            },
        },
        "spec": {
            "project": config.project,
            "source": {
                "repoURL": config.repository_url,
                "path": config.repository_path,
                "targetRevision": config.target_revision,
                "plugin": {
                    "name": "argocd-cyberark-plugin-helm",
                    "env": [
                        {
                            "name": "HELM_ARGS",
                            "value": helm_arguments,
                        }
                    ],
                    "parameters": [
                        {
                            "name": "cyberark",
                            "map": {
                                "appId": cyberark.app_id,
                                "certName": cyberark.cert_name,
                                "keyName": cyberark.key_name,
                                "region": config.region,
                                "safe": cyberark.safe,
                            },
                        }
                    ],
                },
            },
            "destination": {
                "name": config.destination_for(helm_values.environment),
                "namespace": APPLICATION_NAMESPACE,
            },
            "syncPolicy": {
                "automated": {
                    "prune": True,
                    "selfHeal": True,
                }
            },
        },
    }


def _helm_scalar_argument(name: str, value: str) -> str:
    """Build one unquoted Helm scalar assignment."""

    escaped_value = value.replace("\\", "\\\\").replace(",", "\\,")
    return f"--set {name}={escaped_value}"


def application_update_payload(
    existing: Mapping[str, Any],
    desired: Mapping[str, Any],
) -> dict[str, Any]:
    """Preserve server metadata and foreign labels while replacing the desired spec.

    Argo CD owns fields such as resource versions, while Coder Manager owns its
    labels and complete desired spec; this merge keeps that ownership boundary.
    """

    existing_metadata = existing.get("metadata")
    if not isinstance(existing_metadata, Mapping):
        msg = "Argo CD returned an Application without metadata"
        raise ArgoCdRequestError(msg)
    metadata = dict(existing_metadata)
    labels = metadata.get("labels")
    merged_labels = dict(labels) if isinstance(labels, Mapping) else {}
    desired_metadata = desired["metadata"]
    if not isinstance(desired_metadata, Mapping):  # pragma: no cover - internal invariant
        msg = "Invalid desired Application metadata"
        raise ArgoCdRequestError(msg)
    desired_labels = desired_metadata["labels"]
    if not isinstance(desired_labels, Mapping):  # pragma: no cover - internal invariant
        msg = "Invalid desired Application labels"
        raise ArgoCdRequestError(msg)
    merged_labels.update(desired_labels)
    metadata["labels"] = merged_labels
    metadata["name"] = desired_metadata["name"]
    return {
        "apiVersion": existing.get("apiVersion", desired["apiVersion"]),
        "kind": existing.get("kind", desired["kind"]),
        "metadata": metadata,
        "spec": desired["spec"],
    }


def application_status(
    name: str,
    application: Mapping[str, Any],
) -> ArgoCdApplicationStatus:
    """Extract the public remote status fields from an Application payload."""

    return ArgoCdApplicationStatus(
        application_name=name,
        sync_status=_nested_string(application, "status", "sync", "status"),
        health_status=_nested_string(application, "status", "health", "status"),
        operation_phase=_nested_string(application, "status", "operationState", "phase"),
        revision=_nested_string(application, "status", "sync", "revision"),
        reconciled_at=_nested_string(application, "status", "reconciledAt"),
    )


def application_operation_is_active(application: Mapping[str, Any]) -> bool:
    """Return whether Argo CD reports a non-terminal Application operation."""

    phase = _nested_string(application, "status", "operationState", "phase")
    return phase in ACTIVE_OPERATION_PHASES


def _member_values(
    default_admins: Iterable[str],
    members: Iterable[tuple[str, str]],
) -> tuple[list[str], list[str]]:
    """Build deterministic user and administrator lists for Helm values."""

    admins = {ADMIN_USERNAME, *default_admins}
    users = set(admins)
    for username, role in members:
        users.add(username)
        if role == "admin":
            admins.add(username)
    return sorted(users), sorted(admins)


def _nested_string(payload: Mapping[str, Any], *path: str) -> str | None:
    """Read a nested string while tolerating absent or malformed response fields."""

    value: Any = payload
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value if isinstance(value, str) else None
