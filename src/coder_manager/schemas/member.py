"""Instance member request and response schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from coder_manager.models import MemberRole, MemberStatus

MAX_USERNAME_LENGTH = 255


class MemberCreate(BaseModel):
    """Payload accepted when adding a member to an instance."""

    model_config = ConfigDict(extra="forbid")

    username: str
    role: MemberRole

    @field_validator("username")
    @classmethod
    def normalize_username(cls, username: str) -> str:
        """Normalize usernames and reject empty or oversized values."""

        normalized = username.strip().lower()
        if not normalized or len(normalized) > MAX_USERNAME_LENGTH:
            msg = "Username must contain between 1 and 255 characters"
            raise ValueError(msg)
        if "," in normalized:
            msg = "Username cannot contain a comma"
            raise ValueError(msg)
        return normalized


class MemberRoleUpdate(BaseModel):
    """Payload accepted when changing a member's role."""

    model_config = ConfigDict(extra="forbid")

    role: MemberRole


class MemberRead(BaseModel):
    """Instance member representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    instance_id: UUID
    username: str
    role: MemberRole
    action: str
    status: MemberStatus
    created_at: datetime
    updated_at: datetime


class MemberPage(BaseModel):
    """A page of instance members with pagination metadata."""

    items: list[MemberRead]
    page: int
    page_size: int
    total: int
    pages: int
