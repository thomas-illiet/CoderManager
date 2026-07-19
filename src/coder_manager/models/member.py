"""Coder instance member persistence model."""

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coder_manager.models.base import Base

if TYPE_CHECKING:
    from coder_manager.models.instance import Instance
    from coder_manager.models.workspace import Workspace


class MemberRole(StrEnum):
    """Roles supported for an instance member."""

    USER = "user"
    ADMIN = "admin"


class MemberStatus(StrEnum):
    """Execution status of a member's latest action."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


def enum_values(enum_type: type[StrEnum]) -> list[str]:
    """Return enum values for consistent lowercase database persistence."""

    return [member.value for member in enum_type]


class Member(Base):
    """A user membership attached to one Coder instance."""

    __tablename__ = "members"
    __table_args__ = (
        CheckConstraint("length(trim(username)) > 0", name="username_not_empty"),
        CheckConstraint("username = lower(trim(username))", name="username_normalized"),
        CheckConstraint("length(trim(action)) > 0", name="action_not_empty"),
        UniqueConstraint("instance_id", "username", name="uq_members_instance_username"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    instance_id: Mapped[UUID] = mapped_column(
        ForeignKey("instances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[MemberRole] = mapped_column(
        Enum(MemberRole, name="member_role", values_callable=enum_values),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="creating",
        server_default="creating",
    )
    status: Mapped[MemberStatus] = mapped_column(
        Enum(MemberStatus, name="member_status", values_callable=enum_values),
        nullable=False,
        default=MemberStatus.PENDING,
        server_default=MemberStatus.PENDING.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    instance: Mapped["Instance"] = relationship(back_populates="members")
    workspaces: Mapped[list["Workspace"]] = relationship(
        back_populates="member",
        passive_deletes=True,
    )
