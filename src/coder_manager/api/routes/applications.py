"""Application CRUD endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from coder_manager.config import Settings, get_settings
from coder_manager.database import get_session
from coder_manager.repositories import (
    ApplicationAlreadyExistsError,
    ApplicationHasInstancesError,
    ApplicationRepository,
)
from coder_manager.schemas import (
    ApplicationCreate,
    ApplicationListQuery,
    ApplicationPage,
    ApplicationRead,
)

router = APIRouter(prefix="/applications", tags=["applications"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]

WHITELIST_UNAVAILABLE_DETAIL = "Application whitelist management is unavailable"


def application_read(application: object, *, global_whitelist: bool) -> ApplicationRead:
    """Build an API representation using the effective whitelist value."""

    result = ApplicationRead.model_validate(application)
    if global_whitelist:
        return result.model_copy(update={"whitelist": True})
    return result


def ensure_whitelist_management_available(settings: Settings) -> None:
    """Reject individual changes while the global whitelist is active."""

    if settings.global_whitelist:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=WHITELIST_UNAVAILABLE_DETAIL,
        )


@router.get("", summary="List applications")
async def list_applications(
    session: SessionDependency,
    settings: SettingsDependency,
    query: Annotated[ApplicationListQuery, Query()],
) -> ApplicationPage:
    """Return a filtered page ordered by application name and identifier."""

    applications, total = await ApplicationRepository(session).list(
        page=query.page,
        page_size=query.page_size,
        whitelist=query.whitelist,
        name=query.name,
        global_whitelist=settings.global_whitelist,
    )
    pages = (total + query.page_size - 1) // query.page_size
    return ApplicationPage(
        items=[
            application_read(
                application,
                global_whitelist=settings.global_whitelist,
            )
            for application in applications
        ],
        page=query.page,
        page_size=query.page_size,
        total=total,
        pages=pages,
    )


@router.get("/{application_id}", summary="Get an application")
async def get_application(
    application_id: UUID,
    session: SessionDependency,
    settings: SettingsDependency,
) -> ApplicationRead:
    """Return one application or a 404 response."""

    application = await ApplicationRepository(session).get(application_id)
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    return application_read(application, global_whitelist=settings.global_whitelist)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create an application",
)
async def create_application(
    payload: ApplicationCreate,
    session: SessionDependency,
    settings: SettingsDependency,
) -> ApplicationRead:
    """Create an application, rejecting duplicate external identifiers."""

    try:
        application = await ApplicationRepository(session).create(payload)
    except ApplicationAlreadyExistsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An application with this external_id already exists",
        ) from error
    return application_read(application, global_whitelist=settings.global_whitelist)


@router.post(
    "/{application_id}/whitelist",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Whitelist an application",
)
async def whitelist_application(
    application_id: UUID,
    session: SessionDependency,
    settings: SettingsDependency,
) -> Response:
    """Enable an application's individual whitelist flag."""

    ensure_whitelist_management_available(settings)
    repository = ApplicationRepository(session)
    application = await repository.get(application_id)
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    await repository.set_whitelist(application, enabled=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{application_id}/whitelist",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an application from the whitelist",
)
async def unwhitelist_application(
    application_id: UUID,
    session: SessionDependency,
    settings: SettingsDependency,
) -> Response:
    """Disable an application's individual whitelist flag."""

    ensure_whitelist_management_available(settings)
    repository = ApplicationRepository(session)
    application = await repository.get(application_id)
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    await repository.set_whitelist(application, enabled=False)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{application_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an application",
)
async def delete_application(application_id: UUID, session: SessionDependency) -> Response:
    """Delete one application or return a 404 response."""

    repository = ApplicationRepository(session)
    application = await repository.get(application_id)
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    try:
        await repository.delete(application)
    except ApplicationHasInstancesError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Application still has instances",
        ) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
