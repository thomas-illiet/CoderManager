"""Explicit assignment of one Coder template to one instance."""

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
    from coder_manager.models.job_execution import JobExecution
    from coder_manager.models.template import Template
    from coder_manager.models.template_deployment import TemplateDeployment


class TemplateAssignmentStatus(StrEnum):
    """Execution status of a template assignment's latest action."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


def enum_values(enum_type: type[StrEnum]) -> list[str]:
    """Return enum values for consistent lowercase database persistence."""

    return [member.value for member in enum_type]


class TemplateAssignment(Base):
    """Desired presence of one managed template on one Coder instance."""

    __tablename__ = "template_assignments"
    __table_args__ = (
        CheckConstraint(
            "action IN ('creating', 'created', 'deleting')",
            name="action_valid",
        ),
        UniqueConstraint(
            "template_id",
            "instance_id",
            name="uq_template_assignments_template_instance",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    template_id: Mapped[UUID] = mapped_column(
        ForeignKey("templates.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    instance_id: Mapped[UUID] = mapped_column(
        ForeignKey("instances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="creating",
        server_default="creating",
    )
    status: Mapped[TemplateAssignmentStatus] = mapped_column(
        Enum(
            TemplateAssignmentStatus,
            name="template_assignment_status",
            values_callable=enum_values,
        ),
        nullable=False,
        default=TemplateAssignmentStatus.PENDING,
        server_default=TemplateAssignmentStatus.PENDING.value,
    )
    job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("job_executions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    step: Mapped[str | None] = mapped_column(String(255), nullable=True)
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

    template: Mapped["Template"] = relationship(back_populates="assignments")
    instance: Mapped["Instance"] = relationship(back_populates="template_assignments")
    job: Mapped["JobExecution | None"] = relationship(foreign_keys=[job_id])
    deployment: Mapped["TemplateDeployment | None"] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
