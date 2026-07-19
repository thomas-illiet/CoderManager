"""Coder instance persistence model."""

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
    from coder_manager.models.application import Application
    from coder_manager.models.managed_database import DatabaseAllocation
    from coder_manager.models.member import Member
    from coder_manager.models.workspace import Workspace


class InstanceRegion(StrEnum):
    """Regions in which a Coder instance can be provisioned."""

    EMEA = "emea"
    APAC = "apac"
    AMER = "amer"


class InstanceEnvironment(StrEnum):
    """Deployment environments supported by Coder instances."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class InstanceStatus(StrEnum):
    """Execution status of an instance's latest action."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


def enum_values(enum_type: type[StrEnum]) -> list[str]:
    """Return enum values for consistent lowercase database persistence."""

    return [member.value for member in enum_type]


class Instance(Base):
    """A regional Coder instance attached to a business application."""

    __tablename__ = "instances"
    __table_args__ = (
        CheckConstraint("length(trim(action)) > 0", name="action_not_empty"),
        UniqueConstraint(
            "application_id",
            "region",
            "environment",
            name="uq_instances_application_region_environment",
        ),
        UniqueConstraint("instance_url", name="uq_instances_instance_url"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    application_id: Mapped[UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    region: Mapped[InstanceRegion] = mapped_column(
        Enum(
            InstanceRegion,
            name="instance_region",
            values_callable=enum_values,
        ),
        nullable=False,
    )
    environment: Mapped[InstanceEnvironment] = mapped_column(
        Enum(
            InstanceEnvironment,
            name="instance_environment",
            values_callable=enum_values,
        ),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="creating",
        server_default="creating",
    )
    status: Mapped[InstanceStatus] = mapped_column(
        Enum(
            InstanceStatus,
            name="instance_status",
            values_callable=enum_values,
        ),
        nullable=False,
        default=InstanceStatus.PENDING,
        server_default=InstanceStatus.PENDING.value,
    )
    instance_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    argocd_application_name: Mapped[str | None] = mapped_column(
        String(63),
        nullable=True,
        unique=True,
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
    application: Mapped["Application"] = relationship(back_populates="instances")
    members: Mapped[list["Member"]] = relationship(
        back_populates="instance",
        passive_deletes=True,
    )
    workspaces: Mapped[list["Workspace"]] = relationship(
        back_populates="instance",
        passive_deletes=True,
    )
    database_allocation: Mapped["DatabaseAllocation | None"] = relationship(
        back_populates="instance",
        passive_deletes=True,
        uselist=False,
    )

    @property
    def database_id(self) -> UUID | None:
        """Return the database selected for this instance."""

        if self.database_allocation is None:
            return None
        return self.database_allocation.database_id

    @property
    def schema_name(self) -> str | None:
        """Return the reserved schema name for this instance."""

        if self.database_allocation is None:
            return None
        return self.database_allocation.schema_name
