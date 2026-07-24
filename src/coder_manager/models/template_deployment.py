"""Current Coder deployment state for one template and instance."""

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
    from coder_manager.models.template import Template


class TemplateDeploymentStatus(StrEnum):
    """Current synchronization state for one target instance."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


def enum_values(enum_type: type[StrEnum]) -> list[str]:
    """Return enum values for lowercase database persistence."""

    return [member.value for member in enum_type]


class TemplateDeployment(Base):
    """Store only the latest desired and applied state for one target."""

    __tablename__ = "template_deployments"
    __table_args__ = (
        UniqueConstraint(
            "template_id",
            "instance_id",
            name="uq_template_deployments_template_instance",
        ),
        CheckConstraint(
            "target_commit IS NULL OR length(target_commit) = 40",
            name="target_commit_sha",
        ),
        CheckConstraint(
            "applied_commit IS NULL OR length(applied_commit) = 40",
            name="applied_commit_sha",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    template_id: Mapped[UUID] = mapped_column(
        ForeignKey("templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    instance_id: Mapped[UUID] = mapped_column(
        ForeignKey("instances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    coder_organization_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    coder_template_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    coder_template_version_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    target_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    applied_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[TemplateDeploymentStatus] = mapped_column(
        Enum(
            TemplateDeploymentStatus,
            name="template_deployment_status",
            values_callable=enum_values,
        ),
        nullable=False,
        default=TemplateDeploymentStatus.PENDING,
        server_default=TemplateDeploymentStatus.PENDING.value,
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

    template: Mapped["Template"] = relationship(back_populates="deployments")
    instance: Mapped["Instance"] = relationship()
