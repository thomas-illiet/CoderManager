"""Template Docker image request and response schemas."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

ImageValue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class TemplateImageCreate(BaseModel):
    """Payload accepted when allowing an immutable image on a template."""

    model_config = ConfigDict(extra="forbid")

    registry: ImageValue
    name: ImageValue
    version: ImageValue

    @field_validator("registry", "name")
    @classmethod
    def normalize_reference_part(cls, value: str) -> str:
        """Normalize case-insensitive Docker reference components."""

        return value.lower()


class TemplateImageRead(BaseModel):
    """An immutable Docker image allowed by a template."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    template_id: UUID
    registry: str = Field(validation_alias="registry_name")
    name: str
    version: str
    created_at: datetime


class TemplateImagePage(BaseModel):
    """A page of allowed Docker images."""

    items: list[TemplateImageRead]
    page: int
    page_size: int
    total: int
    pages: int
