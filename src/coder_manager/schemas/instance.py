"""Coder instance request and response schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from coder_manager.models import InstanceEnvironment, InstanceRegion, InstanceStatus
from coder_manager.schemas.application_identifier import ApplicationIdentifier


class InstanceCreate(BaseModel):
    """Payload accepted when requesting a new Coder instance."""

    model_config = ConfigDict(extra="forbid")

    application: ApplicationIdentifier
    region: InstanceRegion
    environment: InstanceEnvironment


class InstanceRead(BaseModel):
    """Coder instance representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    application: str
    slug: str | None
    region: InstanceRegion
    environment: InstanceEnvironment
    action: str
    status: InstanceStatus
    instance_url: str
    argocd_application_name: str | None = None
    job_id: UUID | None = None
    step: str | None = None
    database_id: UUID | None = None
    schema_name: str | None = None
    created_at: datetime
    updated_at: datetime


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
