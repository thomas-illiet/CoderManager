"""Coder workspace request and response schemas."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from coder_manager.models import WorkspaceStatus

WorkspaceName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
ModuleName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
PositiveInteger = Annotated[int, Field(gt=0)]


class WorkspaceMutableFields(BaseModel):
    """Validated fields replaceable on an existing workspace."""

    model_config = ConfigDict(extra="forbid")

    name: WorkspaceName
    image_id: UUID
    modules: list[ModuleName]
    cpu: PositiveInteger
    ram: PositiveInteger

    @field_validator("modules")
    @classmethod
    def validate_modules_are_unique(cls, value: list[str]) -> list[str]:
        """Reject duplicate normalized module names while retaining order."""

        if len(value) != len(set(value)):
            msg = "modules must not contain duplicates"
            raise ValueError(msg)
        return value


class WorkspaceCreate(WorkspaceMutableFields):
    """Payload accepted when creating a workspace."""

    instance_id: UUID
    template_id: UUID
    member_id: UUID
    disk: PositiveInteger


class WorkspaceUpdate(WorkspaceMutableFields):
    """Complete replacement of mutable workspace fields."""


class WorkspaceRead(BaseModel):
    """Workspace representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    instance_id: UUID
    template_id: UUID
    member_id: UUID
    image_id: UUID
    modules: list[str]
    cpu: int
    ram: int
    disk: int
    action: str
    status: WorkspaceStatus
    created_at: datetime
    updated_at: datetime


class WorkspaceListQuery(BaseModel):
    """Validated filters and pagination for the workspace list."""

    model_config = ConfigDict(extra="forbid")

    page: Annotated[int, Field(ge=1)] = 1
    page_size: Annotated[int, Field(ge=1, le=100)] = 20
    instance_id: UUID | None = None
    template_id: UUID | None = None
    member_id: UUID | None = None
    image_id: UUID | None = None
    status: WorkspaceStatus | None = None
    name: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    ] = None


class WorkspacePage(BaseModel):
    """A page of workspaces with pagination metadata."""

    items: list[WorkspaceRead]
    page: int
    page_size: int
    total: int
    pages: int
