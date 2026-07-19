"""Application persistence model."""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, String, Uuid, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coder_manager.models.base import Base

if TYPE_CHECKING:
    from coder_manager.models.instance import Instance
    from coder_manager.models.template import Template


class Application(Base):
    """A business application from the company's domain."""

    __tablename__ = "applications"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    whitelist: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    instances: Mapped[list["Instance"]] = relationship(
        back_populates="application",
        passive_deletes=True,
    )
    templates: Mapped[list["Template"]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
    )
