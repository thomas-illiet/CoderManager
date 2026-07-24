"""Kubernetes provider response schema."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class InstanceKubernetesRead(BaseModel):
    """Kubernetes provider status without kubeconfig material."""

    model_config = ConfigDict(from_attributes=True)

    instance_id: UUID
    kubeconfig_configured: bool
    created_at: datetime
    updated_at: datetime
