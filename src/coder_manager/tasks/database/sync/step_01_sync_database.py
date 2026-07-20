"""Synchronize managed databases through the current placeholder."""

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.tasks._common import placeholder
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    complete_execution,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import DATABASE_SYNC_STEP_01_TASK


@celery_app.task(name=DATABASE_SYNC_STEP_01_TASK)
def step_01_sync_database(job_id: str) -> dict[str, str]:
    """Run database synchronization and complete its durable job."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Execute and finalize the synchronization placeholder."""

        placeholder()
        completed = complete_execution(claim, session_factory)
        return {"status": "success" if completed else "noop"}

    return run_claimed_step(job_id, DATABASE_SYNC_STEP_01_TASK, session_factory, operation)
