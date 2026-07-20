"""Drop the PostgreSQL schema owned by a deleting instance."""

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.domains import postgresql
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    advance_execution,
    required_resource_id,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import (
    INSTANCE_DELETE_STEP_03_TASK,
    INSTANCE_DELETE_STEP_04,
    INSTANCE_DELETE_STEP_04_TASK,
)
from coder_manager.tasks.instance._database import database_target


@celery_app.task(name=INSTANCE_DELETE_STEP_03_TASK)
def step_03_remove_schema(job_id: str) -> dict[str, str]:
    """Drop the allocated schema and schedule final local cleanup."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Drop the allocated schema and advance the durable job."""

        target = database_target(required_resource_id(claim), session_factory)
        if target is not None:
            postgresql.drop_schema(target)
        advanced = advance_execution(
            claim,
            next_task_name=INSTANCE_DELETE_STEP_04_TASK,
            next_step=INSTANCE_DELETE_STEP_04,
            session_factory=session_factory,
        )
        return {"status": "pending" if advanced else "noop"}

    return run_claimed_step(job_id, INSTANCE_DELETE_STEP_03_TASK, session_factory, operation)
