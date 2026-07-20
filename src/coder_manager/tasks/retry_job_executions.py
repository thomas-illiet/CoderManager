"""Periodic recovery for pending, failed, and stale background jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TypedDict

from sqlalchemy import or_, select

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.config import get_settings
from coder_manager.models import JobExecution, JobStatus
from coder_manager.tasks.common.execution import prepare_execution_retry
from coder_manager.tasks.common.registry import dispatch_registered_step


class RetryResult(TypedDict):
    """Human-readable summary returned by one recovery scan."""

    status: str
    scheduled: int
    skipped: int


@celery_app.task(name="coder_manager.retry_job_executions")
def retry_job_executions() -> RetryResult:
    """Dispatch every retryable job using its persisted current task name."""

    settings = get_settings()
    session_factory = worker_database.get_worker_session_maker()
    stale_before = datetime.now(UTC) - timedelta(seconds=settings.job_stale_after_seconds)
    with session_factory() as session:
        job_ids = tuple(
            session.scalars(
                select(JobExecution.id)
                .where(
                    or_(
                        JobExecution.status.in_([JobStatus.PENDING, JobStatus.ERROR]),
                        (
                            (JobExecution.status == JobStatus.RUNNING)
                            & (JobExecution.claimed_at <= stale_before)
                        ),
                    )
                )
                .order_by(JobExecution.created_at, JobExecution.id)
            )
        )

    scheduled = 0
    skipped = 0
    for job_id in job_ids:
        task_name = prepare_execution_retry(
            job_id,
            stale_before=stale_before,
            session_factory=session_factory,
        )
        if task_name is None or not dispatch_registered_step(task_name, job_id):
            skipped += 1
        else:
            scheduled += 1
    return {"status": "success", "scheduled": scheduled, "skipped": skipped}
