"""Explicit instance template assignment response schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import AliasPath, BaseModel, ConfigDict, Field

from coder_manager.models import TemplateAssignmentStatus, TemplateDeploymentStatus
from coder_manager.schemas.template import TemplateRead


class InstanceTemplateRead(BaseModel):
    """One template explicitly assigned to a Coder instance."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    instance_id: UUID
    template: TemplateRead
    action: str
    status: TemplateAssignmentStatus
    job_id: UUID | None = None
    step: str | None = None
    deployment_status: TemplateDeploymentStatus | None = Field(
        default=None,
        validation_alias=AliasPath("deployment", "status"),
    )
    target_commit: str | None = Field(
        default=None,
        validation_alias=AliasPath("deployment", "target_commit"),
    )
    applied_commit: str | None = Field(
        default=None,
        validation_alias=AliasPath("deployment", "applied_commit"),
    )
    target_system_parameter_revision: int | None = Field(
        default=None,
        validation_alias=AliasPath("deployment", "target_system_parameter_revision"),
    )
    applied_system_parameter_revision: int | None = Field(
        default=None,
        validation_alias=AliasPath("deployment", "applied_system_parameter_revision"),
    )
    created_at: datetime
    updated_at: datetime


class InstanceTemplatePage(BaseModel):
    """A page of templates explicitly assigned to one instance."""

    items: list[InstanceTemplateRead]
    page: int
    page_size: int
    total: int
    pages: int
