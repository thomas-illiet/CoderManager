"""Create one remote workspace through the current placeholder."""

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.tasks._common import placeholder
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    complete_execution,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import WORKSPACE_CREATE_STEP_01_TASK


@celery_app.task(name=WORKSPACE_CREATE_STEP_01_TASK)
def step_01_create_workspace(job_id: str) -> dict[str, str]:
    """Run workspace creation and finalize its local state."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Run and finalize workspace creation."""

        placeholder()
        completed = complete_execution(claim, session_factory)
        return {"status": "success" if completed else "noop"}

    return run_claimed_step(job_id, WORKSPACE_CREATE_STEP_01_TASK, session_factory, operation)
