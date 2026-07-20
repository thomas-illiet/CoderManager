"""Coder workspace lifecycle endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from coder_manager.database import get_session
from coder_manager.repositories import (
    JobExecutionRepository,
    WorkspaceAlreadyExistsError,
    WorkspaceBusyError,
    WorkspaceConfigurationError,
    WorkspaceImageNotFoundError,
    WorkspaceImageUnavailableError,
    WorkspaceInstanceBusyError,
    WorkspaceInstanceNotFoundError,
    WorkspaceMemberNotFoundError,
    WorkspaceMemberUnavailableError,
    WorkspaceNotFoundError,
    WorkspaceRepository,
    WorkspaceTemplateNotFoundError,
    WorkspaceTemplateUnavailableError,
)
from coder_manager.schemas import (
    JobRead,
    JobResourceResponse,
    WorkspaceCreate,
    WorkspaceListQuery,
    WorkspacePage,
    WorkspaceRead,
    WorkspaceUpdate,
)
from coder_manager.tasks import (
    step_01_create_workspace,
    step_01_delete_workspace,
    step_01_update_workspace,
)
from coder_manager.tasks.common.registry import dispatch_registered_step

router = APIRouter(prefix="/workspaces", tags=["workspaces"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]


@router.get("", summary="List Coder workspaces")
async def list_workspaces(
    session: SessionDependency,
    query: Annotated[WorkspaceListQuery, Query()],
) -> WorkspacePage:
    """Return a filtered deterministic workspace page."""

    workspaces, total = await WorkspaceRepository(session).list_page(query)
    pages = (total + query.page_size - 1) // query.page_size
    return WorkspacePage(
        items=[WorkspaceRead.model_validate(workspace) for workspace in workspaces],
        page=query.page,
        page_size=query.page_size,
        total=total,
        pages=pages,
    )


@router.get("/{workspace_id}", summary="Get a Coder workspace")
async def get_workspace(workspace_id: UUID, session: SessionDependency) -> WorkspaceRead:
    """Return one workspace or a 404 response."""

    workspace = await WorkspaceRepository(session).get(workspace_id)
    if workspace is None:
        raise _workspace_not_found()
    return WorkspaceRead.model_validate(workspace)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a Coder workspace")
async def create_workspace(
    payload: WorkspaceCreate,
    session: SessionDependency,
) -> JobResourceResponse[WorkspaceRead]:
    """Create a workspace after validating all parent and template contracts."""

    try:
        workspace = await WorkspaceRepository(session).create(payload)
    except WorkspaceInstanceNotFoundError as error:
        raise _instance_not_found() from error
    except WorkspaceTemplateNotFoundError as error:
        raise _template_not_found() from error
    except WorkspaceMemberNotFoundError as error:
        raise _member_not_found() from error
    except WorkspaceImageNotFoundError as error:
        raise _image_not_found() from error
    except WorkspaceInstanceBusyError as error:
        raise _instance_busy() from error
    except WorkspaceMemberUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace owner is not ready for this instance",
        ) from error
    except (
        WorkspaceTemplateUnavailableError,
        WorkspaceImageUnavailableError,
        WorkspaceConfigurationError,
    ) as error:
        raise _invalid_configuration() from error
    except WorkspaceAlreadyExistsError as error:
        raise _name_conflict() from error
    job = await _job_read(session, workspace.job_id)
    if job is not None:
        dispatch_registered_step(step_01_create_workspace.name, job.id)
    return JobResourceResponse(resource=WorkspaceRead.model_validate(workspace), job=job)


@router.put("/{workspace_id}", summary="Replace a workspace's mutable fields")
async def update_workspace(
    workspace_id: UUID,
    payload: WorkspaceUpdate,
    session: SessionDependency,
    response: Response,
) -> JobResourceResponse[WorkspaceRead]:
    """Update a workspace or return an unchanged successful no-op."""

    try:
        workspace, changed = await WorkspaceRepository(session).update(workspace_id, payload)
    except WorkspaceNotFoundError as error:
        raise _workspace_not_found() from error
    except WorkspaceInstanceNotFoundError as error:
        raise _instance_not_found() from error
    except WorkspaceTemplateNotFoundError as error:
        raise _template_not_found() from error
    except WorkspaceImageNotFoundError as error:
        raise _image_not_found() from error
    except WorkspaceInstanceBusyError as error:
        raise _instance_busy() from error
    except WorkspaceBusyError as error:
        raise _workspace_busy() from error
    except (WorkspaceImageUnavailableError, WorkspaceConfigurationError) as error:
        raise _invalid_configuration() from error
    except WorkspaceAlreadyExistsError as error:
        raise _name_conflict() from error
    if changed:
        response.status_code = status.HTTP_202_ACCEPTED
    job = await _dispatch_job(
        session,
        workspace.job_id,
        step_01_update_workspace.name,
        dispatch=changed,
    )
    return JobResourceResponse(resource=WorkspaceRead.model_validate(workspace), job=job)


@router.delete(
    "/{workspace_id}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request Coder workspace deletion",
)
async def delete_workspace(
    workspace_id: UUID,
    session: SessionDependency,
) -> JobResourceResponse[WorkspaceRead]:
    """Move an available workspace to deleting/pending."""

    try:
        workspace = await WorkspaceRepository(session).request_deletion(workspace_id)
    except WorkspaceNotFoundError as error:
        raise _workspace_not_found() from error
    except WorkspaceInstanceNotFoundError as error:
        raise _instance_not_found() from error
    except WorkspaceInstanceBusyError as error:
        raise _instance_busy() from error
    except WorkspaceBusyError as error:
        raise _workspace_busy() from error
    job = await _job_read(session, workspace.job_id)
    if job is not None:
        dispatch_registered_step(step_01_delete_workspace.name, job.id)
    return JobResourceResponse(resource=WorkspaceRead.model_validate(workspace), job=job)


async def _job_read(session: AsyncSession, job_id: UUID | None) -> JobRead | None:
    """Load one committed workspace job for its mutation response."""

    if job_id is None:
        return None
    job = await JobExecutionRepository(session).get(job_id)
    return JobRead.model_validate(job) if job is not None else None


async def _dispatch_job(
    session: AsyncSession,
    job_id: UUID | None,
    task_name: str,
    *,
    dispatch: bool,
) -> JobRead | None:
    """Return a job and optionally send its first step."""

    if not dispatch:
        return None
    job = await _job_read(session, job_id)
    if job is not None:
        dispatch_registered_step(task_name, job.id)
    return job


def _workspace_not_found() -> HTTPException:
    """Build the standard response for an unknown workspace."""

    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")


def _instance_not_found() -> HTTPException:
    """Build the standard response for an unknown instance."""

    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instance not found")


def _template_not_found() -> HTTPException:
    """Build the standard response for an unknown template."""

    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")


def _member_not_found() -> HTTPException:
    """Build the standard response for an unknown member."""

    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")


def _image_not_found() -> HTTPException:
    """Build the standard response for an unknown Docker image."""

    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Docker image not found")


def _instance_busy() -> HTTPException:
    """Build the standard response for an instance with an active action."""

    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Instance has an action in progress",
    )


def _workspace_busy() -> HTTPException:
    """Build the standard response for a workspace with an active action."""

    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Workspace has an action in progress",
    )


def _invalid_configuration() -> HTTPException:
    """Build the standard response for a template-incompatible configuration."""

    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="Workspace configuration is incompatible with its template",
    )


def _name_conflict() -> HTTPException:
    """Build the standard response for a duplicate workspace name."""

    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="A workspace with this name already exists in the instance",
    )
