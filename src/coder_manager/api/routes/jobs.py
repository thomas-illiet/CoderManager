"""Durable background job inspection endpoint."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from coder_manager.database import get_session
from coder_manager.repositories import JobExecutionRepository
from coder_manager.schemas import JobRead

router = APIRouter(prefix="/jobs", tags=["jobs"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]


@router.get("/{job_id}", summary="Get a background job")
async def get_job(job_id: UUID, session: SessionDependency) -> JobRead:
    """Return one durable background job or a 404 response."""

    job = await JobExecutionRepository(session).get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return JobRead.model_validate(job)
