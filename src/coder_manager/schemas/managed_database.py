"""Managed database pool request and response schemas."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator

NonEmptyString = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
PASSWORD_MAX_LENGTH = 4096


def validate_password(value: SecretStr) -> SecretStr:
    """Validate length after conversion so errors only contain SecretStr's redacted value."""

    if not 1 <= len(value.get_secret_value()) <= PASSWORD_MAX_LENGTH:
        msg = f"password must contain between 1 and {PASSWORD_MAX_LENGTH} characters"
        raise ValueError(msg)
    return value


class DatabaseMutableFields(BaseModel):
    """Validated public connection and capacity fields."""

    model_config = ConfigDict(extra="forbid")

    name: NonEmptyString
    instance_max: Annotated[int, Field(ge=1)]
    host: NonEmptyString
    port: Annotated[int, Field(ge=1, le=65535)] = 5432
    database_name: NonEmptyString
    username: NonEmptyString


class DatabaseCreate(DatabaseMutableFields):
    """Payload accepted when adding a database to the pool."""

    password: SecretStr

    _validate_password = field_validator("password")(validate_password)


class DatabaseUpdate(DatabaseMutableFields):
    """Complete replacement of public fields with an optional password rotation."""

    password: SecretStr | None = None

    @field_validator("password")
    @classmethod
    def validate_optional_password(cls, value: SecretStr | None) -> SecretStr | None:
        """Validate a supplied rotation password while allowing omission."""

        if value is None:
            return None
        return validate_password(value)


class DatabaseRead(BaseModel):
    """Database representation that never exposes password material."""

    id: UUID
    name: str
    instance_max: int
    host: str
    port: int
    database_name: str
    username: str
    password_configured: bool
    allocated_instances: int
    available_slots: int
    created_at: datetime
    updated_at: datetime


class DatabaseListQuery(BaseModel):
    """Validated filters and pagination for the database list."""

    model_config = ConfigDict(extra="forbid")

    page: Annotated[int, Field(ge=1)] = 1
    page_size: Annotated[int, Field(ge=1, le=100)] = 20
    name: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    ] = None


class DatabasePage(BaseModel):
    """A page of managed databases with usage information."""

    items: list[DatabaseRead]
    page: int
    page_size: int
    total: int
    pages: int


class DatabaseUsageStatistics(BaseModel):
    """Capacity statistics shared by global and per-database views."""

    database_count: int
    total_capacity: int
    allocated_instances: int
    available_slots: int
    utilization_percent: float


class DatabaseItemStatistics(BaseModel):
    """Capacity statistics for one managed database."""

    id: UUID
    name: str
    instance_max: int
    allocated_instances: int
    available_slots: int
    utilization_percent: float


class DatabaseStatistics(DatabaseUsageStatistics):
    """Consolidated pool statistics with per-database detail."""

    databases: list[DatabaseItemStatistics]
