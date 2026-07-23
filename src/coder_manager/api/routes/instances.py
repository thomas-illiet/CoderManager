"""Coder instance lifecycle endpoints."""

from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from coder_manager.config import Settings, get_settings
from coder_manager.crypto import CryptoConfigurationError, KubernetesTokenCipher
from coder_manager.database import get_session
from coder_manager.domains import argocd
from coder_manager.models import InstanceKubernetes
from coder_manager.repositories import (
    InstanceActionConflictError,
    InstanceAlreadyExistsError,
    InstanceDatabaseUnavailableError,
    InstanceKubernetesAlreadyConfiguredError,
    InstanceKubernetesImmutableFieldError,
    InstanceKubernetesNotFoundError,
    InstanceKubernetesRepository,
    InstanceNotFoundError,
    InstanceRepository,
    JobExecutionRepository,
)
from coder_manager.schemas import (
    ApplicationIdentifier,
    InstanceArgoCdStatusRead,
    InstanceCreate,
    InstanceKubernetesCreate,
    InstanceKubernetesRead,
    InstanceKubernetesUpdate,
    InstancePage,
    InstanceRead,
    JobRead,
    JobResourceResponse,
)
from coder_manager.tasks import (
    step_01_create_schema,
    step_01_remove_workspaces,
    step_01_update_instance,
)
from coder_manager.tasks.common.registry import dispatch_registered_step

router = APIRouter(prefix="/instances", tags=["instances"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]


def kubernetes_token_cipher(settings: Settings) -> KubernetesTokenCipher:
    """Build the token cipher or return a redacted configuration error."""

    try:
        return KubernetesTokenCipher(settings.crypto_key)
    except CryptoConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Kubernetes token encryption is not configured",
        ) from error


def kubernetes_provider_read(provider: InstanceKubernetes) -> InstanceKubernetesRead:
    """Build a provider response without exposing encrypted token material."""

    return InstanceKubernetesRead(
        instance_id=provider.instance_id,
        host=provider.host,
        namespace=provider.namespace,
        token_configured=bool(provider.token_enc),
        ca=provider.ca,
        created_at=provider.created_at,
        updated_at=provider.updated_at,
    )


@router.get("", summary="List Coder instances")
async def list_instances(
    session: SessionDependency,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    application: ApplicationIdentifier | None = None,
) -> InstancePage:
    """Return a page of instances, optionally filtered by application."""

    instances, total = await InstanceRepository(session).list(
        page=page,
        page_size=page_size,
        application=application,
    )
    pages = (total + page_size - 1) // page_size
    return InstancePage(
        items=[InstanceRead.model_validate(instance) for instance in instances],
        page=page,
        page_size=page_size,
        total=total,
        pages=pages,
    )


@router.get("/{instance_id}", summary="Get a Coder instance")
async def get_instance(instance_id: UUID, session: SessionDependency) -> InstanceRead:
    """Return one instance or a 404 response."""

    instance = await InstanceRepository(session).get(instance_id)
    if instance is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instance not found")
    return InstanceRead.model_validate(instance)


@router.get(
    "/{instance_id}/provider",
    summary="Get an instance Kubernetes provider",
)
async def get_instance_provider(
    instance_id: UUID,
    session: SessionDependency,
) -> InstanceKubernetesRead:
    """Return the public Kubernetes configuration without its token."""

    try:
        provider = await InstanceKubernetesRepository(session).get(instance_id)
    except InstanceNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Instance not found",
        ) from error
    except InstanceKubernetesNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kubernetes provider not configured",
        ) from error
    return kubernetes_provider_read(provider)


@router.post(
    "/{instance_id}/provider",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Configure an instance Kubernetes provider",
)
async def create_instance_provider(
    instance_id: UUID,
    payload: InstanceKubernetesCreate,
    session: SessionDependency,
    settings: SettingsDependency,
) -> JobResourceResponse[InstanceKubernetesRead]:
    """Create Kubernetes configuration and enqueue an instance reconciliation."""

    try:
        provider = await InstanceKubernetesRepository(session).create_and_request_update(
            instance_id,
            payload,
            kubernetes_token_cipher(settings),
        )
    except InstanceNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Instance not found",
        ) from error
    except InstanceActionConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Instance has an action in progress",
        ) from error
    except InstanceKubernetesAlreadyConfiguredError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Kubernetes provider is already configured",
        ) from error
    job = await _instance_job(session, instance_id)
    if job is not None:
        dispatch_registered_step(step_01_update_instance.name, job.id)
    return JobResourceResponse(resource=kubernetes_provider_read(provider), job=job)


@router.put(
    "/{instance_id}/provider",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Update an instance Kubernetes provider",
)
async def update_instance_provider(
    instance_id: UUID,
    payload: InstanceKubernetesUpdate,
    session: SessionDependency,
    settings: SettingsDependency,
) -> JobResourceResponse[InstanceKubernetesRead]:
    """Update CA or token without allowing host or namespace changes."""

    try:
        provider = await InstanceKubernetesRepository(session).update_and_request_update(
            instance_id,
            payload,
            kubernetes_token_cipher(settings),
        )
    except InstanceNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Instance not found",
        ) from error
    except InstanceKubernetesNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kubernetes provider not configured",
        ) from error
    except InstanceActionConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Instance has an action in progress",
        ) from error
    except InstanceKubernetesImmutableFieldError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Kubernetes provider host and namespace are immutable",
        ) from error
    job = await _instance_job(session, instance_id)
    if job is not None:
        dispatch_registered_step(step_01_update_instance.name, job.id)
    return JobResourceResponse(resource=kubernetes_provider_read(provider), job=job)


@router.get(
    "/{instance_id}/status",
    summary="Get the remote Argo CD status",
)
async def get_instance_status(
    instance_id: UUID,
    session: SessionDependency,
    settings: SettingsDependency,
) -> InstanceArgoCdStatusRead:
    """Return the current sanitized status observed directly from Argo CD."""

    instance = await InstanceRepository(session).get(instance_id)
    if instance is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instance not found")
    try:
        remote = await run_in_threadpool(
            argocd.read_instance_application_status,
            instance.id,
            instance.slug,
            instance.argocd_application_name,
            settings,
        )
    except argocd.ArgoCdApplicationNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Argo CD Application not found",
        ) from error
    except argocd.ArgoCdConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Argo CD is not configured",
        ) from error
    except (argocd.ArgoCdRequestError, httpx.HTTPError) as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Argo CD status is unavailable",
        ) from error
    return InstanceArgoCdStatusRead(
        instance_id=instance.id,
        application_name=remote.application_name,
        sync_status=remote.sync_status,
        health_status=remote.health_status,
        operation_phase=remote.operation_phase,
        revision=remote.revision,
        reconciled_at=remote.reconciled_at,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a Coder instance",
)
async def create_instance(
    payload: InstanceCreate,
    session: SessionDependency,
    settings: SettingsDependency,
) -> JobResourceResponse[InstanceRead]:
    """Create an instance and generate its immutable public URL."""

    try:
        instance = await InstanceRepository(session).create(
            payload,
            instance_domain=settings.instance_domain,
        )
    except InstanceAlreadyExistsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An instance already exists for this placement or slug",
        ) from error
    except InstanceDatabaseUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No database capacity available for region",
        ) from error
    job = await _job_read(session, getattr(instance, "job_id", None))
    if job is not None:
        dispatch_registered_step(step_01_create_schema.name, job.id)
    return JobResourceResponse(resource=InstanceRead.model_validate(instance), job=job)


@router.post(
    "/{instance_id}/sync",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Force an instance synchronization",
)
async def sync_instance(
    instance_id: UUID,
    session: SessionDependency,
) -> JobResourceResponse[InstanceRead]:
    """Request one full Argo CD reconciliation for an idle instance."""

    try:
        instance = await InstanceRepository(session).request_sync(instance_id)
    except InstanceNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Instance not found",
        ) from error
    except InstanceActionConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Instance has an action in progress",
        ) from error
    job = await _job_read(session, getattr(instance, "job_id", None))
    if job is not None:
        dispatch_registered_step(step_01_update_instance.name, job.id)
    return JobResourceResponse(resource=InstanceRead.model_validate(instance), job=job)


@router.delete(
    "/{instance_id}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request Coder instance deletion",
)
async def delete_instance(
    instance_id: UUID,
    session: SessionDependency,
) -> JobResourceResponse[InstanceRead]:
    """Move a successfully reconciled instance to deleting/pending."""

    try:
        instance = await InstanceRepository(session).request_deletion(instance_id)
    except InstanceNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Instance not found",
        ) from error
    except InstanceActionConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a successfully reconciled instance can be deleted",
        ) from error
    job = await _job_read(session, getattr(instance, "job_id", None))
    if job is not None:
        dispatch_registered_step(step_01_remove_workspaces.name, job.id)
    return JobResourceResponse(resource=InstanceRead.model_validate(instance), job=job)


async def _job_read(session: AsyncSession | None, job_id: UUID | None) -> JobRead | None:
    """Load the public durable job representation for a committed transition."""

    if session is None or job_id is None:
        return None
    job = await JobExecutionRepository(session).get(job_id)
    return JobRead.model_validate(job) if job is not None else None


async def _instance_job(session: AsyncSession, instance_id: UUID) -> JobRead | None:
    """Load the current job attached to an instance."""

    instance = await InstanceRepository(session).get(instance_id)
    return await _job_read(session, instance.job_id if instance is not None else None)
