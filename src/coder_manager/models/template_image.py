"""Docker images allowed by a Coder template."""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coder_manager.models.base import Base

if TYPE_CHECKING:
    from coder_manager.models.template import Template
    from coder_manager.models.workspace import Workspace


class TemplateImage(Base):
    """An immutable Docker image allowed for one template."""

    __tablename__ = "template_images"
    __table_args__ = (
        CheckConstraint("length(trim(registry)) > 0", name="registry_not_empty"),
        CheckConstraint("registry = lower(trim(registry))", name="registry_normalized"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_empty"),
        CheckConstraint("name = lower(trim(name))", name="name_normalized"),
        CheckConstraint("length(trim(version)) > 0", name="version_not_empty"),
        UniqueConstraint(
            "template_id",
            "registry",
            "name",
            "version",
            name="uq_template_images_reference",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    template_id: Mapped[UUID] = mapped_column(
        ForeignKey("templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    registry_name: Mapped[str] = mapped_column("registry", String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    template: Mapped["Template"] = relationship(back_populates="images")
    workspaces: Mapped[list["Workspace"]] = relationship(
        back_populates="image",
        passive_deletes=True,
    )
