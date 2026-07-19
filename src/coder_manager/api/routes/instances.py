"""Coder instance lifecycle endpoints."""

from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from coder_manager.config import Settings, get_settings
from coder_manager.database import get_session
from coder_manager.domains import argocd
from coder_manager.repositories import (
    InstanceActionConflictError,
    InstanceAlreadyExistsError,
    InstanceApplicationNotFoundError,
    InstanceApplicationNotWhitelistedError,
    InstanceDatabaseUnavailableError,
    InstanceNotFoundError,
    InstanceRepository,
    InvalidApplicationSlugError,
)
from coder_manager.schemas import (
    InstanceArgoCdStatusRead,
    InstanceCreate,
    InstancePage,
    InstanceRead,
)
from coder_manager.tasks import create_instance as create_instance_job
from coder_manager.tasks import delete_instance as delete_instance_job
from coder_manager.tasks import update_instance as update_instance_job

router = APIRouter(prefix="/instances", tags=["instances"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]


@router.get("", summary="List Coder instances")
async def list_instances(
    session: SessionDependency,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    application_id: UUID | None = None,
) -> InstancePage:
    """Return a page of instances, optionally filtered by application."""

    instances, total = await InstanceRepository(session).list(
        page=page,
        page_size=page_size,
        application_id=application_id,
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
) -> InstanceRead:
    """Create an instance and generate its immutable public URL."""

    try:
        instance = await InstanceRepository(session).create(
            payload,
            global_whitelist=settings.global_whitelist,
        )
    except InstanceApplicationNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found",
        ) from error
    except InstanceApplicationNotWhitelistedError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Application is not whitelisted",
        ) from error
    except InvalidApplicationSlugError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Application name cannot produce a valid DNS label",
        ) from error
    except InstanceAlreadyExistsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An instance already exists for this placement or URL",
        ) from error
    except InstanceDatabaseUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No database capacity available for region",
        ) from error
    create_instance_job.delay(str(instance.id))
    return InstanceRead.model_validate(instance)


@router.post(
    "/{instance_id}/sync",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Force an instance synchronization",
)
async def sync_instance(instance_id: UUID, session: SessionDependency) -> InstanceRead:
    """Request a full Argo CD reconciliation for an idle or failed instance."""

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
    update_instance_job.delay(str(instance.id))
    return InstanceRead.model_validate(instance)


@router.delete(
    "/{instance_id}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request Coder instance deletion",
)
async def delete_instance(instance_id: UUID, session: SessionDependency) -> InstanceRead:
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
    delete_instance_job.delay(str(instance.id))
    return InstanceRead.model_validate(instance)
