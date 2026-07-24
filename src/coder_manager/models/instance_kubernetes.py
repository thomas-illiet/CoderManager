"""Kubernetes provider configuration attached to a Coder instance."""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, LargeBinary, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coder_manager.models.base import Base

if TYPE_CHECKING:
    from coder_manager.models.instance import Instance


class InstanceKubernetes(Base):
    """One Kubernetes provider configuration owned by one Coder instance."""

    __tablename__ = "instance_kubernetes"
    instance_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("instances.id", ondelete="CASCADE"),
        primary_key=True,
    )
    kubeconfig_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
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
    instance: Mapped["Instance"] = relationship(back_populates="kubernetes_provider")
