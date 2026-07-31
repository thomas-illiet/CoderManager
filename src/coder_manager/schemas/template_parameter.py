"""Template parameter request and response schemas."""

import re
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from coder_manager.models import TemplateParameterScope, TemplateParameterType

ParameterName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
DisplayName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]
Description = Annotated[str, StringConstraints(max_length=4096)]
PARAMETER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
ENVIRONMENTS = frozenset({"development", "staging", "production"})


class ParameterNamedFields(BaseModel):
    """Common create fields for one named parameter."""

    model_config = ConfigDict(extra="forbid")

    name: ParameterName
    display_name: DisplayName
    description: Description = ""

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        """Normalize and validate the stable parameter identifier."""

        if PARAMETER_NAME_PATTERN.fullmatch(value) is None:
            msg = "name must be a lowercase snake_case identifier"
            raise ValueError(msg)
        return value


class UserTemplateParameterCreate(ParameterNamedFields):
    """Create one workspace-provided template parameter."""

    type: Literal[TemplateParameterType.USER]
    required: bool
    mutable: bool
    default_value: str | None = None


class SystemTemplateParameterCreate(ParameterNamedFields):
    """Create one encrypted system-provided template parameter."""

    type: Literal[TemplateParameterType.SYSTEM]
    scope: TemplateParameterScope
    value: str | None = None
    values: dict[str, str] | None = None

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        """Require exactly the value shape selected by the system scope."""

        if self.scope is TemplateParameterScope.GLOBAL:
            valid = self.value is not None and self.values is None
        else:
            valid = (
                self.value is None and self.values is not None and set(self.values) == ENVIRONMENTS
            )
        if not valid:
            msg = "system parameter values must exactly match scope"
            raise ValueError(msg)
        return self


TemplateParameterCreate = Annotated[
    UserTemplateParameterCreate | SystemTemplateParameterCreate,
    Field(discriminator="type"),
]


class ParameterUpdateFields(BaseModel):
    """Common mutable presentation fields."""

    model_config = ConfigDict(extra="forbid")

    display_name: DisplayName
    description: Description = ""


class UserTemplateParameterUpdate(ParameterUpdateFields):
    """Replace one user parameter's mutable definition."""

    type: Literal[TemplateParameterType.USER]
    required: bool
    mutable: bool
    default_value: str | None = None


class SystemTemplateParameterUpdate(ParameterUpdateFields):
    """Replace one system parameter while optionally rotating its secret values."""

    type: Literal[TemplateParameterType.SYSTEM]
    scope: TemplateParameterScope
    value: str | None = None
    values: dict[str, str] | None = None

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        """Allow omission, otherwise require the complete selected value shape."""

        if self.value is None and self.values is None:
            return self
        if self.scope is TemplateParameterScope.GLOBAL:
            valid = self.value is not None and self.values is None
        else:
            valid = (
                self.value is None and self.values is not None and set(self.values) == ENVIRONMENTS
            )
        if not valid:
            msg = "system parameter values must exactly match scope"
            raise ValueError(msg)
        return self


TemplateParameterUpdate = Annotated[
    UserTemplateParameterUpdate | SystemTemplateParameterUpdate,
    Field(discriminator="type"),
]


class TemplateParameterRead(BaseModel):
    """Redacted representation of one template parameter."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    template_id: UUID
    type: TemplateParameterType
    name: str
    display_name: str
    description: str
    required: bool | None
    mutable: bool | None
    default_value: str | None
    scope: TemplateParameterScope | None
    value_configured: bool | None = None
    values_configured: dict[str, bool] | None = None
    created_at: datetime
    updated_at: datetime


class TemplateParameterPage(BaseModel):
    """A page of template parameters."""

    items: list[TemplateParameterRead]
    page: int
    page_size: int
    total: int
    pages: int
