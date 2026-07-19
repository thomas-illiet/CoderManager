"""Application request and response schemas."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyString = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]


class ApplicationCreate(BaseModel):
    """Payload accepted when creating an application."""

    external_id: NonEmptyString
    name: NonEmptyString


class ApplicationRead(BaseModel):
    """Application representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    external_id: str
    name: str
    whitelist: bool
    created_at: datetime


class ApplicationListQuery(BaseModel):
    """Validated filters and pagination for the application list."""

    page: Annotated[int, Field(ge=1)] = 1
    page_size: Annotated[int, Field(ge=1, le=100)] = 20
    whitelist: bool | None = None
    name: Annotated[str | None, Field(min_length=1, max_length=255)] = None


class ApplicationPage(BaseModel):
    """A page of applications with pagination metadata."""

    items: list[ApplicationRead]
    page: int
    page_size: int
    total: int
    pages: int
