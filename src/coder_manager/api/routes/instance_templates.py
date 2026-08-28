"""Explicit template assignment endpoints for Coder instances."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from coder_manager.database import get_session
from coder_manager.repositories import (
    JobExecutionRepository,
    TemplateAssignmentActionConflictError,
    TemplateAssignmentInstanceNotFoundError,
    TemplateAssignmentInstanceUnavailableError,
    TemplateAssignmentJobConflictError,
    TemplateAssignmentRepository,
    TemplateAssignmentSyncInProgressError,
    TemplateAssignmentTemplateNotFoundError,
    TemplateAssignmentWorkspacesBusyError,
)
from coder_manager.schemas import (
    InstanceTemplatePage,
    InstanceTemplateRead,
    JobRead,
    JobResourceResponse,
)
from coder_manager.tasks.common.registry import (
    TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
    TEMPLATE_ASSIGNMENT_DELETE_STEP_01_TASK,
    dispatch_registered_step,
)

router = APIRouter(
    prefix="/instances/{instance_id}/templates",
    tags=["instance templates"],
)
SessionDependency = Annotated[AsyncSession, Depends(get_session)]


@router.get("", summary="List templates assigned to an instance")
async def list_instance_templates(
    instance_id: UUID,
    session: SessionDependency,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> InstanceTemplatePage:
    """Return a deterministic assignment page even while the instance is unavailable."""

    try:
        assignments, total = await TemplateAssignmentRepository(session).list(
            instance_id,
            page=page,
            page_size=page_size,
        )
    except TemplateAssignmentInstanceNotFoundError as error:
        raise _instance_not_found() from error
    pages = (total + page_size - 1) // page_size
    return InstanceTemplatePage(
        items=[InstanceTemplateRead.model_validate(item) for item in assignments],
        page=page,
        page_size=page_size,
        total=total,
        pages=pages,
    )


@router.put(
    "/{template_id}",
    summary="Assign a template to an instance",
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": JobResourceResponse[InstanceTemplateRead],
            "description": "Assignment creation is pending, running, or retryable.",
        }
    },
)
async def put_instance_template(
    instance_id: UUID,
    template_id: UUID,
    response: Response,
    session: SessionDependency,
) -> JobResourceResponse[InstanceTemplateRead]:
    """Create an assignment or return its idempotent asynchronous/current state."""

    try:
        assignment, accepted = await TemplateAssignmentRepository(session).put(
            instance_id,
            template_id,
        )
    except TemplateAssignmentInstanceNotFoundError as error:
        raise _instance_not_found() from error
    except TemplateAssignmentTemplateNotFoundError as error:
        raise _template_not_found() from error
    except TemplateAssignmentInstanceUnavailableError as error:
        raise _instance_unavailable() from error
    except TemplateAssignmentSyncInProgressError as error:
        raise _sync_conflict() from error
    except TemplateAssignmentActionConflictError as error:
        raise _action_conflict() from error
    except TemplateAssignmentJobConflictError as error:
        raise _job_conflict() from error

    if accepted:
        response.status_code = status.HTTP_202_ACCEPTED
    job = await _job_read(session, assignment.job_id) if accepted else None
    _dispatch_enqueued_job(session, TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK)
    return JobResourceResponse(
        resource=InstanceTemplateRead.model_validate(assignment),
        job=job,
    )


@router.delete(
    "/{template_id}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobResourceResponse[InstanceTemplateRead],
    responses={
        status.HTTP_204_NO_CONTENT: {
            "description": "The template is already absent from the instance.",
        }
    },
    summary="Remove a template from an instance",
)
async def delete_instance_template(
    instance_id: UUID,
    template_id: UUID,
    session: SessionDependency,
) -> JobResourceResponse[InstanceTemplateRead] | Response:
    """Request removal or return an empty idempotent response when already absent."""

    try:
        assignment, _accepted = await TemplateAssignmentRepository(session).request_deletion(
            instance_id,
            template_id,
        )
    except TemplateAssignmentInstanceNotFoundError as error:
        raise _instance_not_found() from error
    except TemplateAssignmentTemplateNotFoundError as error:
        raise _template_not_found() from error
    except TemplateAssignmentInstanceUnavailableError as error:
        raise _instance_unavailable() from error
    except TemplateAssignmentSyncInProgressError as error:
        raise _sync_conflict() from error
    except TemplateAssignmentActionConflictError as error:
        raise _action_conflict() from error
    except TemplateAssignmentJobConflictError as error:
        raise _job_conflict() from error
    except TemplateAssignmentWorkspacesBusyError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Template assignment has workspaces with an action in progress",
        ) from error

    if assignment is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    job = await _job_read(session, assignment.job_id)
    _dispatch_enqueued_job(session, TEMPLATE_ASSIGNMENT_DELETE_STEP_01_TASK)
    return JobResourceResponse(
        resource=InstanceTemplateRead.model_validate(assignment),
        job=job,
    )


async def _job_read(session: AsyncSession, job_id: UUID | None) -> JobRead | None:
    """Load the durable job currently owned by an assignment."""

    if job_id is None:
        return None
    job = await JobExecutionRepository(session).get(job_id)
    return JobRead.model_validate(job) if job is not None else None


def _dispatch_enqueued_job(session: AsyncSession, task_name: str) -> None:
    """Dispatch only a job created by the current repository transition."""

    job_id = session.info.pop("enqueue_job_id", None)
    if isinstance(job_id, UUID):
        dispatch_registered_step(task_name, job_id)


def _instance_not_found() -> HTTPException:
    """Build the standard unknown-instance response."""

    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instance not found")


def _template_not_found() -> HTTPException:
    """Build the standard unknown-template response."""

    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")


def _instance_unavailable() -> HTTPException:
    """Build the response for an instance that cannot accept assignment mutations."""

    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Instance must be started and ready",
    )


def _sync_conflict() -> HTTPException:
    """Build the active template-wide synchronization response."""

    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Template synchronization is already in progress",
    )


def _action_conflict() -> HTTPException:
    """Build the incompatible assignment transition response."""

    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Template assignment has a conflicting action",
    )


def _job_conflict() -> HTTPException:
    """Build the inconsistent durable assignment job response."""

    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Template assignment durable job is inconsistent",
    )
