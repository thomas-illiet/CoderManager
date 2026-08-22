"""Coder instance request and response schemas."""

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from coder_manager.models import Instance, InstanceEnvironment, InstanceState, InstanceStatus
from coder_manager.schemas.application_identifier import ApplicationIdentifier
from coder_manager.utils.instance_urls import InstancePublicUrlConfig


class InstanceCreate(BaseModel):
    """Payload accepted when requesting a new Coder instance."""

    model_config = ConfigDict(extra="forbid")

    application: ApplicationIdentifier
    environment: InstanceEnvironment


class InstanceRead(BaseModel):
    """Coder instance representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    application: str
    slug: str
    environment: InstanceEnvironment
    action: str
    status: InstanceStatus
    state: InstanceState
    instance_url: str
    argocd_application_name: str | None = None
    job_id: UUID | None = None
    step: str | None = None
    database_id: UUID | None = None
    schema_name: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_instance(
        cls,
        instance: Instance,
        public_url_config: InstancePublicUrlConfig,
    ) -> Self:
        """Map one stored instance and runtime URL configuration to its API representation."""

        return cls(
            id=instance.id,
            application=instance.application,
            slug=instance.slug,
            environment=instance.environment,
            action=instance.action,
            status=instance.status,
            state=instance.state,
            instance_url=public_url_config.url_for(instance.slug, instance.environment),
            argocd_application_name=instance.argocd_application_name,
            job_id=getattr(instance, "job_id", None),
            step=getattr(instance, "step", None),
            database_id=getattr(instance, "database_id", None),
            schema_name=getattr(instance, "schema_name", None),
            created_at=instance.created_at,
            updated_at=instance.updated_at,
        )


class InstancePage(BaseModel):
    """A page of Coder instances with pagination metadata."""

    items: list[InstanceRead]
    page: int
    page_size: int
    total: int
    pages: int


class InstanceArgoCdStatusRead(BaseModel):
    """Sanitized remote Argo CD status for one managed instance."""

    model_config = ConfigDict(from_attributes=True)

    instance_id: UUID
    application_name: str
    sync_status: str | None = None
    health_status: str | None = None
    operation_phase: str | None = None
    revision: str | None = None
    reconciled_at: datetime | None = None


class InstanceAdminCredentialsRead(BaseModel):
    """Static administrator identity and decrypted instance password."""

    username: str
    email: str
    password: str
