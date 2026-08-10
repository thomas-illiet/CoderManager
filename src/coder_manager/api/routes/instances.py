"""Coder instance lifecycle endpoints."""

from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from coder_manager.config import Settings, get_settings
from coder_manager.crypto import (
    CryptoConfigurationError,
    InstancePasswordCipher,
    InstancePasswordDecryptionError,
    KubeconfigCipher,
    KubeconfigDecryptionError,
)
from coder_manager.database import get_session
from coder_manager.domains import argocd
from coder_manager.domains.coder import ADMIN_EMAIL, ADMIN_USERNAME
from coder_manager.models import InstanceKubernetes
from coder_manager.repositories import (
    InstanceActionConflictError,
    InstanceAlreadyExistsError,
    InstanceDatabaseUnavailableError,
    InstanceKubernetesAlreadyConfiguredError,
    InstanceKubernetesNotFoundError,
    InstanceKubernetesRepository,
    InstanceNotFoundError,
    InstanceRepository,
    JobExecutionRepository,
)
from coder_manager.schemas import (
    ApplicationIdentifier,
    InstanceAdminCredentialsRead,
    InstanceArgoCdStatusRead,
    InstanceCreate,
    InstanceKubernetesRead,
    InstancePage,
    InstanceRead,
    JobRead,
    JobResourceResponse,
)
from coder_manager.tasks import (
    step_01_create_schema,
    step_01_remove_workspaces,
    step_01_start_instance,
    step_01_stop_workspaces,
    step_01_update_instance,
)
from coder_manager.tasks.common.registry import dispatch_registered_step

router = APIRouter(prefix="/instances", tags=["instances"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]
NO_STORE_HEADERS = {"Cache-Control": "no-store"}


def kubeconfig_cipher(settings: Settings) -> KubeconfigCipher:
    """Build the kubeconfig cipher or return a redacted configuration error."""

    try:
        return KubeconfigCipher(settings.crypto_key)
    except CryptoConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Kubeconfig encryption is not configured",
            headers=NO_STORE_HEADERS,
        ) from error


def instance_password_cipher(settings: Settings) -> InstancePasswordCipher:
    """Build the instance password cipher or return a redacted configuration error."""

    try:
        return InstancePasswordCipher(settings.crypto_key)
    except CryptoConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Instance password encryption is not configured",
            headers=NO_STORE_HEADERS,
        ) from error


def kubernetes_provider_read(provider: InstanceKubernetes) -> InstanceKubernetesRead:
    """Build a provider response without exposing kubeconfig material."""

    return InstanceKubernetesRead(
        instance_id=provider.instance_id,
        kubeconfig_configured=True,
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
    "/{instance_id}/admin",
    summary="Get an instance administrator account",
)
async def get_instance_admin(
    instance_id: UUID,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
) -> InstanceAdminCredentialsRead:
    """Return the static administrator identity and decrypted stored password."""

    instance = await InstanceRepository(session).get(instance_id)
    if instance is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Instance not found",
            headers=NO_STORE_HEADERS,
        )
    if instance.password_enc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Instance admin account not initialized",
            headers=NO_STORE_HEADERS,
        )
    try:
        password = instance_password_cipher(settings).decrypt(
            instance.password_enc,
            instance.id,
        )
    except InstancePasswordDecryptionError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Instance admin password cannot be decrypted",
            headers=NO_STORE_HEADERS,
        ) from error
    response.headers.update(NO_STORE_HEADERS)
    return InstanceAdminCredentialsRead(
        username=ADMIN_USERNAME,
        email=ADMIN_EMAIL,
        password=password.get_secret_value(),
    )


@router.get(
    "/{instance_id}/provider",
    summary="Get an instance Kubernetes provider",
)
async def get_instance_provider(
    instance_id: UUID,
    session: SessionDependency,
) -> InstanceKubernetesRead:
    """Return Kubernetes provider status without its kubeconfig."""

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


@router.get(
    "/{instance_id}/provider/configuration",
    summary="Download an instance Kubernetes provider configuration",
)
async def get_instance_provider_configuration(
    instance_id: UUID,
    session: SessionDependency,
    settings: SettingsDependency,
) -> Response:
    """Return the decrypted kubeconfig as a non-cacheable binary download."""

    try:
        provider = await InstanceKubernetesRepository(session).get(instance_id)
    except InstanceNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Instance not found",
            headers=NO_STORE_HEADERS,
        ) from error
    except InstanceKubernetesNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kubernetes provider not configured",
            headers=NO_STORE_HEADERS,
        ) from error
    try:
        kubeconfig = kubeconfig_cipher(settings).decrypt(
            provider.kubeconfig_enc,
            provider.instance_id,
        )
    except KubeconfigDecryptionError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Kubeconfig cannot be decrypted",
            headers=NO_STORE_HEADERS,
        ) from error
    return Response(
        content=kubeconfig,
        media_type="application/octet-stream",
        headers={
            **NO_STORE_HEADERS,
            "Content-Disposition": 'attachment; filename="kubeconfig"',
        },
    )


@router.post(
    "/{instance_id}/provider",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Configure an instance Kubernetes provider",
)
async def create_instance_provider(
    instance_id: UUID,
    kubeconfig: Annotated[UploadFile, File()],
    session: SessionDependency,
    settings: SettingsDependency,
) -> JobResourceResponse[InstanceKubernetesRead]:
    """Upload a kubeconfig and enqueue an instance reconciliation."""

    try:
        provider = await InstanceKubernetesRepository(session).create_and_request_update(
            instance_id,
            await kubeconfig.read(),
            kubeconfig_cipher(settings),
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
            instance.slug,
            instance.argocd_application_name,
            instance.environment.value,
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
            detail="No database capacity available",
        ) from error
    job = await _job_read(session, getattr(instance, "job_id", None))
    if job is not None:
        dispatch_registered_step(step_01_create_schema.name, job.id)
    return JobResourceResponse(resource=InstanceRead.model_validate(instance), job=job)


@router.post(
    "/{instance_id}/start",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a Coder instance",
)
async def start_instance(
    instance_id: UUID,
    session: SessionDependency,
) -> JobResourceResponse[InstanceRead]:
    """Request a strict full reconciliation for one idle instance."""

    try:
        instance = await InstanceRepository(session).request_start(instance_id)
    except InstanceNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Instance not found",
        ) from error
    except InstanceActionConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Instance has an action in progress or is deleting",
        ) from error
    job = await _job_read(session, instance.job_id)
    if job is not None:
        dispatch_registered_step(step_01_start_instance.name, job.id)
    return JobResourceResponse(resource=InstanceRead.model_validate(instance), job=job)


@router.post(
    "/{instance_id}/stop",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Stop a Coder instance",
)
async def stop_instance(
    instance_id: UUID,
    session: SessionDependency,
) -> JobResourceResponse[InstanceRead]:
    """Stop active workspaces before deleting only the remote Application."""

    try:
        instance = await InstanceRepository(session).request_stop(instance_id)
    except InstanceNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Instance not found",
        ) from error
    except InstanceActionConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Instance has an action in progress or is deleting",
        ) from error
    job = await _job_read(session, instance.job_id)
    if job is not None:
        dispatch_registered_step(step_01_stop_workspaces.name, job.id)
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
