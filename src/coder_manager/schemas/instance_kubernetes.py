"""Kubernetes provider request and response schemas."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr, StringConstraints, field_validator

KUBERNETES_TOKEN_MAX_LENGTH = 65536
KUBERNETES_CA_MAX_LENGTH = 1048576
KubernetesHost = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2048),
]
KubernetesNamespace = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$",
    ),
]
KubernetesCa = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=KUBERNETES_CA_MAX_LENGTH),
]


def validate_token(value: SecretStr | None) -> SecretStr | None:
    """Validate token length without exposing its plaintext in validation errors."""

    if value is None:
        return None
    if not 1 <= len(value.get_secret_value()) <= KUBERNETES_TOKEN_MAX_LENGTH:
        msg = f"token must contain between 1 and {KUBERNETES_TOKEN_MAX_LENGTH} characters"
        raise ValueError(msg)
    return value


class InstanceKubernetesCreate(BaseModel):
    """Complete configuration required to create a Kubernetes provider."""

    model_config = ConfigDict(extra="forbid")

    host: KubernetesHost
    namespace: KubernetesNamespace
    token: SecretStr
    ca: KubernetesCa

    _validate_token = field_validator("token")(validate_token)


class InstanceKubernetesUpdate(BaseModel):
    """Mutable provider fields with optional immutable-field assertions."""

    model_config = ConfigDict(extra="forbid")

    host: KubernetesHost | None = None
    namespace: KubernetesNamespace | None = None
    token: SecretStr | None = None
    ca: KubernetesCa

    _validate_token = field_validator("token")(validate_token)


class InstanceKubernetesRead(BaseModel):
    """Kubernetes provider configuration without token material."""

    model_config = ConfigDict(from_attributes=True)

    instance_id: UUID
    host: str
    namespace: str
    token_configured: bool
    ca: str
    created_at: datetime
    updated_at: datetime
