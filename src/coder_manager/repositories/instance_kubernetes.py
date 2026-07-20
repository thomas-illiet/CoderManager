"""Persistence operations for instance Kubernetes provider configurations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from coder_manager.models import Instance, InstanceKubernetes, InstanceStatus
from coder_manager.repositories.instances import (
    InstanceActionConflictError,
    InstanceNotFoundError,
)
from coder_manager.repositories.job_executions import add_job_execution
from coder_manager.tasks.common.registry import (
    INSTANCE_UPDATE_STEP_01,
    INSTANCE_UPDATE_STEP_01_TASK,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from coder_manager.crypto import KubernetesTokenCipher
    from coder_manager.schemas import InstanceKubernetesCreate, InstanceKubernetesUpdate


class InstanceKubernetesNotFoundError(Exception):
    """Raised when an instance has no Kubernetes provider configuration."""


class InstanceKubernetesAlreadyConfiguredError(Exception):
    """Raised when creating a provider that is already configured."""


class InstanceKubernetesImmutableFieldError(Exception):
    """Raised when an update attempts to change host or namespace."""


class InstanceKubernetesRepository:
    """Store the one-to-one Kubernetes provider configuration for an instance."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the database session used by repository operations."""

        self._session = session

    async def get(self, instance_id: UUID) -> InstanceKubernetes:
        """Return one provider configuration while distinguishing a missing instance."""

        provider = await self._session.get(InstanceKubernetes, instance_id)
        if provider is not None:
            return provider
        if await self._session.get(Instance, instance_id) is None:
            raise InstanceNotFoundError
        raise InstanceKubernetesNotFoundError

    async def _lock_idle_instance(self, instance_id: UUID) -> Instance:
        """Lock an existing idle instance for a provider mutation."""

        instance = await self._session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if instance is None:
            await self._session.rollback()
            raise InstanceNotFoundError
        if instance.status is not InstanceStatus.SUCCESS:
            await self._session.rollback()
            raise InstanceActionConflictError
        return instance

    async def create_and_request_update(
        self,
        instance_id: UUID,
        payload: InstanceKubernetesCreate,
        cipher: KubernetesTokenCipher,
    ) -> InstanceKubernetes:
        """Create provider data and atomically request an instance reconciliation."""

        instance = await self._lock_idle_instance(instance_id)
        if await self._session.get(InstanceKubernetes, instance_id) is not None:
            await self._session.rollback()
            raise InstanceKubernetesAlreadyConfiguredError

        provider = InstanceKubernetes(
            instance_id=instance_id,
            host=payload.host,
            namespace=payload.namespace,
            token_enc=cipher.encrypt(payload.token, instance_id),
            ca=payload.ca,
        )
        self._session.add(provider)
        instance.action = "updating"
        instance.status = InstanceStatus.PENDING
        job = add_job_execution(
            self._session,
            name="instance.update",
            task_name=INSTANCE_UPDATE_STEP_01_TASK,
            resource_type="instance",
            resource_id=instance.id,
            step=INSTANCE_UPDATE_STEP_01,
        )
        instance.job_id = job.id
        instance.step = INSTANCE_UPDATE_STEP_01
        await self._session.commit()
        await self._session.refresh(provider)
        return provider

    async def update_and_request_update(
        self,
        instance_id: UUID,
        payload: InstanceKubernetesUpdate,
        cipher: KubernetesTokenCipher,
    ) -> InstanceKubernetes:
        """Update mutable provider data and atomically request reconciliation."""

        instance = await self._lock_idle_instance(instance_id)
        provider = await self._session.get(InstanceKubernetes, instance_id)
        if provider is None:
            await self._session.rollback()
            raise InstanceKubernetesNotFoundError
        if (payload.host is not None and payload.host != provider.host) or (
            payload.namespace is not None and payload.namespace != provider.namespace
        ):
            await self._session.rollback()
            raise InstanceKubernetesImmutableFieldError

        provider.ca = payload.ca
        if payload.token is not None:
            provider.token_enc = cipher.encrypt(payload.token, instance_id)
        provider.updated_at = datetime.now(UTC)

        instance.action = "updating"
        instance.status = InstanceStatus.PENDING
        job = add_job_execution(
            self._session,
            name="instance.update",
            task_name=INSTANCE_UPDATE_STEP_01_TASK,
            resource_type="instance",
            resource_id=instance.id,
            step=INSTANCE_UPDATE_STEP_01,
        )
        instance.job_id = job.id
        instance.step = INSTANCE_UPDATE_STEP_01
        await self._session.commit()
        await self._session.refresh(provider)
        return provider
