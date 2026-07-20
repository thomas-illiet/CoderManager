"""Instance member lifecycle endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from coder_manager.database import get_session
from coder_manager.repositories import (
    JobExecutionRepository,
    MemberActionConflictError,
    MemberAlreadyExistsError,
    MemberHasWorkspacesError,
    MemberInstanceBusyError,
    MemberInstanceNotFoundError,
    MemberNotFoundError,
    MemberRepository,
)
from coder_manager.schemas import (
    JobRead,
    JobResourceResponse,
    MemberCreate,
    MemberPage,
    MemberRead,
    MemberRoleUpdate,
)
from coder_manager.tasks import step_01_update_instance
from coder_manager.tasks.common.registry import dispatch_registered_step

router = APIRouter(prefix="/instances/{instance_id}/members", tags=["members"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]


@router.get("", summary="List instance members")
async def list_members(
    instance_id: UUID,
    session: SessionDependency,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MemberPage:
    """Return a page of members belonging to one instance."""

    try:
        members, total = await MemberRepository(session).list(
            instance_id,
            page=page,
            page_size=page_size,
        )
    except MemberInstanceNotFoundError as error:
        raise _instance_not_found() from error
    pages = (total + page_size - 1) // page_size
    return MemberPage(
        items=[MemberRead.model_validate(member) for member in members],
        page=page,
        page_size=page_size,
        total=total,
        pages=pages,
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="Add an instance member")
async def create_member(
    instance_id: UUID,
    payload: MemberCreate,
    session: SessionDependency,
) -> JobResourceResponse[MemberRead]:
    """Add a normalized username in the creating/pending state."""

    try:
        member = await MemberRepository(session).create(instance_id, payload)
    except MemberInstanceNotFoundError as error:
        raise _instance_not_found() from error
    except MemberInstanceBusyError as error:
        raise _instance_busy() from error
    except MemberAlreadyExistsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Member already exists for this instance",
        ) from error
    job = await _consume_instance_update_request(session)
    if job is not None:
        dispatch_registered_step(step_01_update_instance.name, job.id)
    return JobResourceResponse(resource=MemberRead.model_validate(member), job=job)


@router.get("/{member_id}", summary="Get an instance member")
async def get_member(
    instance_id: UUID,
    member_id: UUID,
    session: SessionDependency,
) -> MemberRead:
    """Return one member constrained to its parent instance."""

    member = await MemberRepository(session).get(instance_id, member_id)
    if member is None:
        raise _member_not_found()
    return MemberRead.model_validate(member)


@router.put("/{member_id}", summary="Change an instance member role")
async def update_member_role(
    instance_id: UUID,
    member_id: UUID,
    payload: MemberRoleUpdate,
    session: SessionDependency,
    response: Response,
) -> JobResourceResponse[MemberRead]:
    """Request a role update or return the successful no-op unchanged."""

    try:
        member, changed = await MemberRepository(session).update_role(
            instance_id,
            member_id,
            payload,
        )
    except MemberInstanceNotFoundError as error:
        raise _instance_not_found() from error
    except MemberInstanceBusyError as error:
        raise _instance_busy() from error
    except MemberNotFoundError as error:
        raise _member_not_found() from error
    except MemberActionConflictError as error:
        raise _member_conflict() from error
    if changed:
        response.status_code = status.HTTP_202_ACCEPTED
    job = await _consume_instance_update_request(session) if changed else None
    if job is not None:
        dispatch_registered_step(step_01_update_instance.name, job.id)
    return JobResourceResponse(resource=MemberRead.model_validate(member), job=job)


@router.delete(
    "/{member_id}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request instance member deletion",
)
async def delete_member(
    instance_id: UUID,
    member_id: UUID,
    session: SessionDependency,
) -> JobResourceResponse[MemberRead]:
    """Move a successfully processed member to deleting/pending."""

    try:
        member = await MemberRepository(session).request_deletion(instance_id, member_id)
    except MemberInstanceNotFoundError as error:
        raise _instance_not_found() from error
    except MemberInstanceBusyError as error:
        raise _instance_busy() from error
    except MemberNotFoundError as error:
        raise _member_not_found() from error
    except MemberActionConflictError as error:
        raise _member_conflict() from error
    except MemberHasWorkspacesError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Member still owns workspaces",
        ) from error
    job = await _consume_instance_update_request(session)
    if job is not None:
        dispatch_registered_step(step_01_update_instance.name, job.id)
    return JobResourceResponse(resource=MemberRead.model_validate(member), job=job)


async def _consume_instance_update_request(session: AsyncSession | None) -> JobRead | None:
    """Consume the post-commit dispatch signal emitted by the repository."""

    if session is None:
        return None
    job_id = session.info.pop("enqueue_job_id", None)
    if not isinstance(job_id, UUID):
        return None
    job = await JobExecutionRepository(session).get(job_id)
    return JobRead.model_validate(job) if job is not None else None


def _instance_not_found() -> HTTPException:
    """Build the standard response for an unknown instance."""

    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instance not found")


def _member_not_found() -> HTTPException:
    """Build the standard response for an unknown member."""

    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")


def _instance_busy() -> HTTPException:
    """Build the standard response for an instance with an active action."""

    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Instance has an action in progress",
    )


def _member_conflict() -> HTTPException:
    """Build the standard response for an invalid member lifecycle transition."""

    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Member action is not in a successful state",
    )
