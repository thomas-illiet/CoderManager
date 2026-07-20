"""Background job API schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from coder_manager.models import JobStatus


class JobRead(BaseModel):
    """Public state of one durable background job."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    resource_type: str | None = None
    resource_id: UUID | None = None
    step: str
    status: JobStatus
    attempt: int
    claimed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class JobResponse(BaseModel):
    """Response for an asynchronous operation without a resource payload."""

    job: JobRead


class JobResourceResponse[ResourceT](BaseModel):
    """Response pairing an affected resource with its durable job."""

    resource: ResourceT
    job: JobRead | None
