"""Placeholder for removing remote workspaces before instance deletion."""

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.tasks._common import placeholder
from coder_manager.tasks.common.execution import ExecutionClaim, advance_execution, run_claimed_step
from coder_manager.tasks.common.registry import (
    INSTANCE_DELETE_STEP_01_TASK,
    INSTANCE_DELETE_STEP_02,
    INSTANCE_DELETE_STEP_02_TASK,
)


@celery_app.task(name=INSTANCE_DELETE_STEP_01_TASK)
def step_01_remove_workspaces(job_id: str) -> dict[str, str]:
    """Run the workspace-removal placeholder and schedule remote instance deletion."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Run the placeholder and advance the durable job."""

        placeholder()
        advanced = advance_execution(
            claim,
            next_task_name=INSTANCE_DELETE_STEP_02_TASK,
            next_step=INSTANCE_DELETE_STEP_02,
            session_factory=session_factory,
        )
        return {"status": "pending" if advanced else "noop"}

    return run_claimed_step(job_id, INSTANCE_DELETE_STEP_01_TASK, session_factory, operation)
