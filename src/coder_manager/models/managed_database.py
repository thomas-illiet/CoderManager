"""Managed PostgreSQL database pool persistence models."""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coder_manager.models.base import Base

if TYPE_CHECKING:
    from coder_manager.models.instance import Instance


class Database(Base):
    """A PostgreSQL database that can host multiple isolated Coder schemas."""

    __tablename__ = "databases"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="name_not_empty"),
        CheckConstraint("instance_max > 0", name="instance_max_positive"),
        CheckConstraint("length(trim(host)) > 0", name="host_not_empty"),
        CheckConstraint("port >= 1 AND port <= 65535", name="port_valid"),
        CheckConstraint("length(trim(database_name)) > 0", name="database_name_not_empty"),
        CheckConstraint("length(trim(username)) > 0", name="username_not_empty"),
        Index("uq_databases_name_ci", text("lower(name)"), unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    instance_max: Mapped[int] = mapped_column(Integer, nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=5432, server_default="5432")
    database_name: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
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
    allocations: Mapped[list["DatabaseAllocation"]] = relationship(
        back_populates="database",
        passive_deletes=True,
    )


class DatabaseAllocation(Base):
    """Reservation of one schema slot for one Coder instance."""

    __tablename__ = "database_allocations"
    __table_args__ = (
        CheckConstraint("length(trim(schema_name)) > 0", name="schema_name_not_empty"),
        UniqueConstraint("instance_id", name="uq_database_allocations_instance_id"),
        UniqueConstraint(
            "database_id",
            "schema_name",
            name="uq_database_allocations_database_schema",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    database_id: Mapped[UUID] = mapped_column(
        ForeignKey("databases.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    instance_id: Mapped[UUID] = mapped_column(
        ForeignKey("instances.id", ondelete="RESTRICT"),
        nullable=False,
    )
    schema_name: Mapped[str] = mapped_column(String(63), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    database: Mapped[Database] = relationship(back_populates="allocations")
    instance: Mapped["Instance"] = relationship(back_populates="database_allocation")
