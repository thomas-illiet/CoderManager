"""Coder template persistence model."""

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coder_manager.models.base import Base

if TYPE_CHECKING:
    from coder_manager.models.job_execution import JobExecution
    from coder_manager.models.template_deployment import TemplateDeployment
    from coder_manager.models.template_image import TemplateImage
    from coder_manager.models.template_parameter import TemplateParameter
    from coder_manager.models.workspace import Workspace


class TemplateScope(StrEnum):
    """Scopes in which a Coder template can be used."""

    GLOBAL = "global"
    APPLICATION = "application"


class TemplateSyncStatus(StrEnum):
    """Current state of the template's fire-and-forget synchronization."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


def enum_values(enum_type: type[StrEnum]) -> list[str]:
    """Return enum values for consistent lowercase database persistence."""

    return [member.value for member in enum_type]


class Template(Base):
    """A branch-backed Coder template available globally or to one application."""

    __tablename__ = "templates"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[TemplateScope] = mapped_column(
        Enum(TemplateScope, name="template_scope", values_callable=enum_values),
        nullable=False,
    )
    application: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        index=True,
    )
    git_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False, default=".")
    branch: Mapped[str] = mapped_column(String(255), nullable=False)
    modules: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    system_parameter_revision: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default="0",
    )
    action: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="created",
        server_default="created",
    )
    sync_status: Mapped[TemplateSyncStatus] = mapped_column(
        Enum(
            TemplateSyncStatus,
            name="template_sync_status",
            values_callable=enum_values,
        ),
        nullable=False,
        default=TemplateSyncStatus.SUCCESS,
        server_default=TemplateSyncStatus.SUCCESS.value,
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
    images: Mapped[list["TemplateImage"]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    workspaces: Mapped[list["Workspace"]] = relationship(
        back_populates="template",
        passive_deletes=True,
    )
    deployments: Mapped[list["TemplateDeployment"]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    parameters: Mapped[list["TemplateParameter"]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    job: Mapped["JobExecution | None"] = relationship(foreign_keys=[job_id])

    __table_args__ = (
        CheckConstraint("length(trim(display_name)) > 0", name="display_name_not_empty"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_empty"),
        CheckConstraint("name = lower(trim(name))", name="name_normalized"),
        CheckConstraint("length(trim(git_url)) > 0", name="git_url_not_empty"),
        CheckConstraint("length(trim(source_path)) > 0", name="source_path_not_empty"),
        CheckConstraint("length(trim(branch)) > 0", name="branch_not_empty"),
        CheckConstraint("length(trim(action)) > 0", name="action_not_empty"),
        CheckConstraint(
            "system_parameter_revision >= 0",
            name="system_parameter_revision_non_negative",
        ),
        CheckConstraint(
            "(scope = 'global' AND application IS NULL) OR "
            "(scope = 'application' AND application IS NOT NULL)",
            name="scope_application_consistent",
        ),
        CheckConstraint(
            "application IS NULL OR (length(trim(application)) > 0 "
            "AND application = upper(trim(application)))",
            name="application_normalized",
        ),
        Index(
            "uq_templates_global_display_name_ci",
            func.lower(display_name),
            unique=True,
            postgresql_where=scope == TemplateScope.GLOBAL,
            sqlite_where=scope == TemplateScope.GLOBAL,
        ),
        Index(
            "uq_templates_application_display_name_ci",
            application,
            func.lower(display_name),
            unique=True,
            postgresql_where=scope == TemplateScope.APPLICATION,
            sqlite_where=scope == TemplateScope.APPLICATION,
        ),
        Index(
            "uq_templates_global_name_ci",
            func.lower(name),
            unique=True,
            postgresql_where=scope == TemplateScope.GLOBAL,
            sqlite_where=scope == TemplateScope.GLOBAL,
        ),
        Index(
            "uq_templates_application_name_ci",
            application,
            func.lower(name),
            unique=True,
            postgresql_where=scope == TemplateScope.APPLICATION,
            sqlite_where=scope == TemplateScope.APPLICATION,
        ),
    )
