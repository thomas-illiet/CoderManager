"""Template parameter definitions and encrypted system values."""

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coder_manager.models.base import Base

if TYPE_CHECKING:
    from coder_manager.models.template import Template


class TemplateParameterType(StrEnum):
    """Supported parameter ownership categories."""

    USER = "user"
    SYSTEM = "system"


class TemplateParameterScope(StrEnum):
    """Supported scopes for system parameter values."""

    GLOBAL = "global"
    ENVIRONMENT = "environment"


class TemplateParameterValueTarget(StrEnum):
    """Concrete target attached to one encrypted system value."""

    GLOBAL = "global"
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


def enum_values(enum_type: type[StrEnum]) -> list[str]:
    """Return enum values for lowercase database persistence."""

    return [member.value for member in enum_type]


class TemplateParameter(Base):
    """One user or system parameter owned by a Coder template."""

    __tablename__ = "template_parameters"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="name_not_empty"),
        CheckConstraint("name = lower(trim(name))", name="name_normalized"),
        CheckConstraint("length(trim(display_name)) > 0", name="display_name_not_empty"),
        CheckConstraint(
            "(type = 'user' AND scope IS NULL AND required IS NOT NULL "
            "AND mutable IS NOT NULL) OR "
            "(type = 'system' AND scope IS NOT NULL AND required IS NULL "
            "AND mutable IS NULL AND default_value IS NULL)",
            name="type_fields_consistent",
        ),
        UniqueConstraint("template_id", "name", name="uq_template_parameters_template_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    template_id: Mapped[UUID] = mapped_column(
        ForeignKey("templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[TemplateParameterType] = mapped_column(
        Enum(
            TemplateParameterType,
            name="template_parameter_type",
            values_callable=enum_values,
        ),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mutable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    default_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[TemplateParameterScope | None] = mapped_column(
        Enum(
            TemplateParameterScope,
            name="template_parameter_scope",
            values_callable=enum_values,
        ),
        nullable=True,
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

    template: Mapped["Template"] = relationship(back_populates="parameters")
    system_values: Mapped[list["TemplateParameterSystemValue"]] = relationship(
        back_populates="parameter",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TemplateParameterSystemValue.target",
    )


class TemplateParameterSystemValue(Base):
    """One encrypted system parameter value for a concrete target."""

    __tablename__ = "template_parameter_system_values"

    parameter_id: Mapped[UUID] = mapped_column(
        ForeignKey("template_parameters.id", ondelete="CASCADE"),
        primary_key=True,
    )
    target: Mapped[TemplateParameterValueTarget] = mapped_column(
        Enum(
            TemplateParameterValueTarget,
            name="template_parameter_value_target",
            values_callable=enum_values,
        ),
        primary_key=True,
    )
    value_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    parameter: Mapped["TemplateParameter"] = relationship(back_populates="system_values")
