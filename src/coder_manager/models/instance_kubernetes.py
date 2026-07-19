"""Kubernetes provider configuration attached to a Coder instance."""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, LargeBinary, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coder_manager.models.base import Base

if TYPE_CHECKING:
    from coder_manager.models.instance import Instance


class InstanceKubernetes(Base):
    """One Kubernetes provider configuration owned by one Coder instance."""

    __tablename__ = "instance_kubernetes"
    __table_args__ = (
        CheckConstraint("length(trim(host)) > 0", name="host_not_empty"),
        CheckConstraint("length(trim(namespace)) > 0", name="namespace_not_empty"),
        CheckConstraint("length(trim(ca)) > 0", name="ca_not_empty"),
    )

    instance_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("instances.id", ondelete="CASCADE"),
        primary_key=True,
    )
    host: Mapped[str] = mapped_column(String(2048), nullable=False)
    namespace: Mapped[str] = mapped_column(String(63), nullable=False)
    token_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ca: Mapped[str] = mapped_column(Text, nullable=False)
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
