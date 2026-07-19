"""Coder template persistence model."""

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, Enum, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coder_manager.models.base import Base

if TYPE_CHECKING:
    from coder_manager.models.application import Application
    from coder_manager.models.template_image import TemplateImage
    from coder_manager.models.workspace import Workspace


class TemplateScope(StrEnum):
    """Scopes in which a Coder template can be used."""

    GLOBAL = "global"
    APPLICATION = "application"


def enum_values(enum_type: type[StrEnum]) -> list[str]:
    """Return enum values for consistent lowercase database persistence."""

    return [member.value for member in enum_type]


class Template(Base):
    """A versioned Coder template available globally or to one application."""

    __tablename__ = "templates"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[TemplateScope] = mapped_column(
        Enum(TemplateScope, name="template_scope", values_callable=enum_values),
        nullable=False,
    )
    application_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    git_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    modules: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    version: Mapped[str] = mapped_column(String(255), nullable=False)
    min_cpu_count: Mapped[int] = mapped_column(nullable=False)
    max_cpu_count: Mapped[int] = mapped_column(nullable=False)
    min_ram_gb: Mapped[int] = mapped_column(nullable=False)
    max_ram_gb: Mapped[int] = mapped_column(nullable=False)
    min_disk_gb: Mapped[int] = mapped_column(nullable=False)
    max_disk_gb: Mapped[int] = mapped_column(nullable=False)
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
    application: Mapped["Application | None"] = relationship(back_populates="templates")
    images: Mapped[list["TemplateImage"]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    workspaces: Mapped[list["Workspace"]] = relationship(
        back_populates="template",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="name_not_empty"),
        CheckConstraint("length(trim(git_url)) > 0", name="git_url_not_empty"),
        CheckConstraint("length(trim(version)) > 0", name="version_not_empty"),
        CheckConstraint("min_cpu_count > 0", name="min_cpu_count_positive"),
        CheckConstraint("max_cpu_count > 0", name="max_cpu_count_positive"),
        CheckConstraint("min_cpu_count <= max_cpu_count", name="cpu_range_valid"),
        CheckConstraint("min_ram_gb > 0", name="min_ram_gb_positive"),
        CheckConstraint("max_ram_gb > 0", name="max_ram_gb_positive"),
        CheckConstraint("min_ram_gb <= max_ram_gb", name="ram_range_valid"),
        CheckConstraint("min_disk_gb > 0", name="min_disk_gb_positive"),
        CheckConstraint("max_disk_gb > 0", name="max_disk_gb_positive"),
        CheckConstraint("min_disk_gb <= max_disk_gb", name="disk_range_valid"),
        CheckConstraint(
            "(scope = 'global' AND application_id IS NULL) OR "
            "(scope = 'application' AND application_id IS NOT NULL)",
            name="scope_application_consistent",
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
            application_id,
            func.lower(name),
            unique=True,
            postgresql_where=scope == TemplateScope.APPLICATION,
            sqlite_where=scope == TemplateScope.APPLICATION,
        ),
    )
