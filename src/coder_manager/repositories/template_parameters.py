"""Persistence operations for parameters owned by templates."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from coder_manager.models import (
    Template,
    TemplateAssignment,
    TemplateAssignmentStatus,
    TemplateParameter,
    TemplateParameterSystemValue,
    TemplateParameterType,
    TemplateSyncStatus,
)
from coder_manager.schemas import (
    SystemTemplateParameterCreate,
    SystemTemplateParameterUpdate,
    UserTemplateParameterCreate,
    UserTemplateParameterUpdate,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from coder_manager.crypto import TemplateParameterCipher
    from coder_manager.schemas import TemplateParameterCreate, TemplateParameterUpdate


class TemplateParameterTemplateNotFoundError(Exception):
    """Raised when a parameter operation references an unknown template."""


class TemplateParameterNotFoundError(Exception):
    """Raised when a parameter is not present in the requested template."""


class TemplateParameterAlreadyExistsError(Exception):
    """Raised when a parameter name already exists in one template."""


class TemplateParameterImmutableFieldError(Exception):
    """Raised when a parameter type would change."""


class TemplateParameterSyncInProgressError(Exception):
    """Raised when a template synchronization owns the parameter snapshot."""


class TemplateParameterRepository:
    """Store and retrieve user and encrypted system template parameters."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the database session used by repository operations."""

        self._session = session

    async def list(
        self,
        template_id: UUID,
        *,
        page: int,
        page_size: int,
    ) -> tuple[Sequence[TemplateParameter], int]:
        """Return one deterministic parameter page after validating its template."""

        if await self._session.get(Template, template_id) is None:
            raise TemplateParameterTemplateNotFoundError
        condition = TemplateParameter.template_id == template_id
        total = await self._session.scalar(
            select(func.count()).select_from(TemplateParameter).where(condition)
        )
        result = await self._session.scalars(
            select(TemplateParameter)
            .options(selectinload(TemplateParameter.system_value))
            .where(condition)
            .order_by(TemplateParameter.type, TemplateParameter.name, TemplateParameter.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(result), total or 0

    async def get(self, template_id: UUID, parameter_id: UUID) -> TemplateParameter | None:
        """Find one parameter constrained to its parent template."""

        return await self._session.scalar(
            select(TemplateParameter)
            .options(selectinload(TemplateParameter.system_value))
            .where(
                TemplateParameter.id == parameter_id,
                TemplateParameter.template_id == template_id,
            )
        )

    async def create(
        self,
        template_id: UUID,
        payload: TemplateParameterCreate,
        cipher: TemplateParameterCipher | None,
    ) -> TemplateParameter:
        """Create one parameter and atomically advance system configuration state."""

        template = await self._lock_template(template_id)
        parameter, _ = self._new_parameter(
            template,
            payload,
            cipher,
        )
        self._session.add(parameter)
        try:
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise TemplateParameterAlreadyExistsError from error
        return await self._required_parameter(template_id, parameter.id)

    @staticmethod
    def _new_parameter(
        template: Template,
        payload: TemplateParameterCreate,
        cipher: TemplateParameterCipher | None,
    ) -> tuple[TemplateParameter, TemplateParameterSystemValue | None]:
        """Build one definition and its optional encrypted value without database I/O."""

        parameter = TemplateParameter(
            id=uuid4(),
            template_id=template.id,
            type=payload.type,
            name=payload.name,
            display_name=payload.display_name,
            description=payload.description,
        )
        if isinstance(payload, UserTemplateParameterCreate):
            parameter.required = payload.required
            parameter.mutable = payload.mutable
            parameter.default_value = payload.default_value
        elif isinstance(payload, SystemTemplateParameterCreate):
            if cipher is None:
                msg = "Template parameter encryption is required"
                raise RuntimeError(msg)
            system_value = TemplateParameterSystemValue(
                parameter_id=parameter.id,
                value_enc=cipher.encrypt(payload.value, parameter.id),
            )
            parameter.system_value = system_value
            template.system_parameter_revision += 1
            return parameter, system_value
        return parameter, None

    async def update(
        self,
        template_id: UUID,
        parameter_id: UUID,
        payload: TemplateParameterUpdate,
        cipher: TemplateParameterCipher | None,
    ) -> TemplateParameter:
        """Replace mutable fields while preserving type and name."""

        template = await self._lock_template(template_id)
        parameter = await self._locked_parameter(template_id, parameter_id)
        try:
            self._apply_update(template, parameter, payload, cipher)
        except TemplateParameterImmutableFieldError:
            await self._session.rollback()
            raise
        await self._session.commit()
        return await self._required_parameter(template_id, parameter.id)

    @staticmethod
    def _apply_update(
        template: Template,
        parameter: TemplateParameter,
        payload: TemplateParameterUpdate,
        cipher: TemplateParameterCipher | None,
    ) -> None:
        """Apply one validated replacement and advance effective system state."""

        if parameter.type is not payload.type:
            raise TemplateParameterImmutableFieldError
        changed = (
            parameter.display_name != payload.display_name
            or parameter.description != payload.description
        )
        parameter.display_name = payload.display_name
        parameter.description = payload.description
        system_changed = False
        if isinstance(payload, UserTemplateParameterUpdate):
            user_changed = (
                parameter.required != payload.required
                or parameter.mutable != payload.mutable
                or parameter.default_value != payload.default_value
            )
            parameter.required = payload.required
            parameter.mutable = payload.mutable
            parameter.default_value = payload.default_value
            changed = changed or user_changed
        elif isinstance(payload, SystemTemplateParameterUpdate):
            if payload.value is not None:
                if cipher is None:
                    msg = "Template parameter encryption is required"
                    raise RuntimeError(msg)
                stored_value = parameter.system_value
                if stored_value is None:
                    parameter.system_value = TemplateParameterSystemValue(
                        parameter_id=parameter.id,
                        value_enc=cipher.encrypt(payload.value, parameter.id),
                    )
                    template.system_parameter_revision += 1
                    system_changed = True
                elif cipher.decrypt(stored_value.value_enc, parameter.id) != payload.value:
                    stored_value.value_enc = cipher.encrypt(payload.value, parameter.id)
                    template.system_parameter_revision += 1
                    system_changed = True
            changed = changed or system_changed

        if changed:
            parameter.updated_at = datetime.now(UTC)

    async def delete(self, template_id: UUID, parameter_id: UUID) -> None:
        """Delete one parameter without rewriting existing workspace snapshots."""

        template = await self._lock_template(template_id)
        parameter = await self._locked_parameter(template_id, parameter_id)
        if parameter.type is TemplateParameterType.SYSTEM:
            template.system_parameter_revision += 1
        await self._session.delete(parameter)
        await self._session.commit()

    async def _lock_template(self, template_id: UUID) -> Template:
        """Lock one idle template for a parameter mutation."""

        template = await self._session.scalar(
            select(Template).where(Template.id == template_id).with_for_update()
        )
        if template is None:
            await self._session.rollback()
            raise TemplateParameterTemplateNotFoundError
        if template.sync_status in {
            TemplateSyncStatus.PENDING,
            TemplateSyncStatus.RUNNING,
            TemplateSyncStatus.ERROR,
        }:
            await self._session.rollback()
            raise TemplateParameterSyncInProgressError
        assignment_id = await self._session.scalar(
            select(TemplateAssignment.id)
            .where(
                TemplateAssignment.template_id == template_id,
                TemplateAssignment.status.in_(
                    {
                        TemplateAssignmentStatus.PENDING,
                        TemplateAssignmentStatus.RUNNING,
                        TemplateAssignmentStatus.ERROR,
                    }
                ),
            )
            .limit(1)
        )
        if assignment_id is not None:
            await self._session.rollback()
            raise TemplateParameterSyncInProgressError
        return template

    async def _locked_parameter(
        self,
        template_id: UUID,
        parameter_id: UUID,
    ) -> TemplateParameter:
        """Lock and eagerly load one parameter in its template."""

        parameter = await self._session.scalar(
            select(TemplateParameter)
            .options(selectinload(TemplateParameter.system_value))
            .where(
                TemplateParameter.id == parameter_id,
                TemplateParameter.template_id == template_id,
            )
            .with_for_update()
        )
        if parameter is None:
            await self._session.rollback()
            raise TemplateParameterNotFoundError
        return parameter

    async def _required_parameter(
        self,
        template_id: UUID,
        parameter_id: UUID,
    ) -> TemplateParameter:
        """Reload one parameter after a committed mutation."""

        parameter = await self.get(template_id, parameter_id)
        if parameter is None:  # pragma: no cover - committed invariant
            raise TemplateParameterNotFoundError
        return parameter
