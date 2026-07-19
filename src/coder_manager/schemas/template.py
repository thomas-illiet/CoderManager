"""Coder template request and response schemas."""

from datetime import datetime
from typing import Annotated, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from coder_manager.models import TemplateScope

NonEmptyString = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
GitUrl = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
ModuleName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
ModuleList = Annotated[list[ModuleName], Field(min_length=1)]
PositiveInteger = Annotated[int, Field(gt=0)]


class TemplateMutableFields(BaseModel):
    """Validated fields that can be replaced on an existing template."""

    model_config = ConfigDict(extra="forbid")

    name: NonEmptyString
    git_url: GitUrl
    modules: ModuleList
    version: NonEmptyString
    min_cpu_count: PositiveInteger
    max_cpu_count: PositiveInteger
    min_ram_gb: PositiveInteger
    max_ram_gb: PositiveInteger
    min_disk_gb: PositiveInteger
    max_disk_gb: PositiveInteger

    @field_validator("git_url")
    @classmethod
    def validate_git_url(cls, value: str) -> str:
        """Require an absolute HTTPS URL without contacting the repository."""

        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname is None:
            msg = "git_url must be an absolute HTTPS URL"
            raise ValueError(msg)
        return value

    @field_validator("modules")
    @classmethod
    def validate_modules_are_unique(cls, value: list[str]) -> list[str]:
        """Reject duplicate normalized module names while retaining their order."""

        if len(value) != len(set(value)):
            msg = "modules must not contain duplicates"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_resource_ranges(self) -> Self:
        """Require coherent inclusive resource ranges."""

        ranges = (
            ("cpu", self.min_cpu_count, self.max_cpu_count),
            ("ram", self.min_ram_gb, self.max_ram_gb),
            ("disk", self.min_disk_gb, self.max_disk_gb),
        )
        for resource, minimum, maximum in ranges:
            if minimum > maximum:
                msg = f"min_{resource} must be less than or equal to max_{resource}"
                raise ValueError(msg)
        return self


class TemplateCreate(TemplateMutableFields):
    """Payload accepted when creating a Coder template."""

    scope: TemplateScope
    application_id: UUID | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        """Ensure the application reference agrees with the selected scope."""

        if self.scope is TemplateScope.GLOBAL and self.application_id is not None:
            msg = "application_id must be null for a global template"
            raise ValueError(msg)
        if self.scope is TemplateScope.APPLICATION and self.application_id is None:
            msg = "application_id is required for an application template"
            raise ValueError(msg)
        return self


class TemplateUpdate(TemplateMutableFields):
    """Complete replacement of a template's mutable fields."""


class TemplateRead(BaseModel):
    """Coder template representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    scope: TemplateScope
    application_id: UUID | None
    git_url: str
    modules: list[str]
    version: str
    min_cpu_count: int
    max_cpu_count: int
    min_ram_gb: int
    max_ram_gb: int
    min_disk_gb: int
    max_disk_gb: int
    created_at: datetime
    updated_at: datetime


class TemplateListQuery(BaseModel):
    """Validated filters and pagination for the template list."""

    model_config = ConfigDict(extra="forbid")

    page: Annotated[int, Field(ge=1)] = 1
    page_size: Annotated[int, Field(ge=1, le=100)] = 20
    scope: TemplateScope | None = None
    application_id: UUID | None = None
    name: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    ] = None


class TemplatePage(BaseModel):
    """A page of Coder templates with pagination metadata."""

    items: list[TemplateRead]
    page: int
    page_size: int
    total: int
    pages: int
