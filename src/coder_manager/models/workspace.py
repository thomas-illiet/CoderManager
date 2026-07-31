"""Coder workspace persistence model."""

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, Enum, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coder_manager.models.base import Base

if TYPE_CHECKING:
    from coder_manager.models.instance import Instance
    from coder_manager.models.job_execution import JobExecution
    from coder_manager.models.member import Member
    from coder_manager.models.template import Template
    from coder_manager.models.template_image import TemplateImage


class WorkspaceStatus(StrEnum):
    """Execution status of a workspace's latest action."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


def enum_values(enum_type: type[StrEnum]) -> list[str]:
    """Return enum values for consistent lowercase database persistence."""

    return [member.value for member in enum_type]


class Workspace(Base):
    """A managed Coder workspace attached to one instance and owner."""

    __tablename__ = "workspaces"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    instance_id: Mapped[UUID] = mapped_column(
        ForeignKey("instances.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template_id: Mapped[UUID] = mapped_column(
        ForeignKey("templates.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    member_id: Mapped[UUID] = mapped_column(
        ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True
    )
    image_id: Mapped[UUID] = mapped_column(
        ForeignKey("template_images.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    modules: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    parameters: Mapped[dict[str, str]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    parameters_revision: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default="0",
    )
    applied_parameters_revision: Mapped[int | None] = mapped_column(nullable=True)
    coder_workspace_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    coder_workspace_build_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    action: Mapped[str] = mapped_column(
        String(255), nullable=False, default="creating", server_default="creating"
    )
    status: Mapped[WorkspaceStatus] = mapped_column(
        Enum(WorkspaceStatus, name="workspace_status", values_callable=enum_values),
        nullable=False,
        default=WorkspaceStatus.PENDING,
        server_default=WorkspaceStatus.PENDING.value,
    )
    job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("job_executions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    step: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    instance: Mapped["Instance"] = relationship(back_populates="workspaces")
    template: Mapped["Template"] = relationship(back_populates="workspaces")
    member: Mapped["Member"] = relationship(back_populates="workspaces")
    image: Mapped["TemplateImage"] = relationship(back_populates="workspaces")
    job: Mapped["JobExecution | None"] = relationship(foreign_keys=[job_id])

    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="name_not_empty"),
        CheckConstraint("length(name) <= 32", name="name_length"),
        CheckConstraint("length(trim(action)) > 0", name="action_not_empty"),
        CheckConstraint("parameters_revision >= 0", name="parameters_revision_non_negative"),
        CheckConstraint(
            "applied_parameters_revision IS NULL OR applied_parameters_revision >= 0",
            name="applied_parameters_revision_non_negative",
        ),
        Index("uq_workspaces_instance_name_ci", instance_id, func.lower(name), unique=True),
    )
