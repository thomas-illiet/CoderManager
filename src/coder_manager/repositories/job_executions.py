"""Persistence helpers for durable background jobs."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import select

from coder_manager.models import JobExecution, JobStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class JobExecutionNotFoundError(Exception):
    """Raised when a durable job cannot be found."""


def add_job_execution(  # noqa: PLR0913
    session: AsyncSession,
    *,
    name: str,
    task_name: str,
    resource_type: str | None,
    resource_id: UUID | None,
    step: str,
) -> JobExecution:
    """Stage a pending job in the caller's current transaction."""

    job = JobExecution(
        id=uuid4(),
        name=name,
        task_name=task_name,
        resource_type=resource_type,
        resource_id=resource_id,
        step=step,
        status=JobStatus.PENDING,
    )
    session.add(job)
    return job


class JobExecutionRepository:
    """Read and create durable job executions through an async session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the database session used by repository operations."""

        self._session = session

    async def get(self, job_id: UUID) -> JobExecution | None:
        """Return one job by identifier."""

        return await self._session.get(JobExecution, job_id)

    async def has_successful_resource_step(
        self,
        *,
        resource_type: str,
        resource_id: UUID,
        step: str,
    ) -> bool:
        """Report whether a resource has completed one exact durable step."""

        job_id = await self._session.scalar(
            select(JobExecution.id)
            .where(
                JobExecution.resource_type == resource_type,
                JobExecution.resource_id == resource_id,
                JobExecution.step == step,
                JobExecution.status == JobStatus.SUCCESS,
            )
            .limit(1)
        )
        return job_id is not None

    async def create_system_job(self, *, name: str, task_name: str, step: str) -> JobExecution:
        """Create and commit a job that is not attached to a resource."""

        job = add_job_execution(
            self._session,
            name=name,
            task_name=task_name,
            resource_type=None,
            resource_id=None,
            step=step,
        )
        await self._session.commit()
        await self._session.refresh(job)
        return job
