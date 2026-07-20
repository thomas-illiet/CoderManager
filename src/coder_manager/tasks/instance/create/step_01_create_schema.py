"""Create the PostgreSQL schema allocated to a new instance."""

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
    INSTANCE_CREATE_STEP_01_TASK,
    INSTANCE_CREATE_STEP_02,
    INSTANCE_CREATE_STEP_02_TASK,
)
from coder_manager.tasks.instance._database import database_target


@celery_app.task(name=INSTANCE_CREATE_STEP_01_TASK)
def step_01_create_schema(job_id: str) -> dict[str, str]:
    """Create the allocated schema, then directly schedule instance creation."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Create the schema and persist the next step."""

        target = database_target(required_resource_id(claim), session_factory)
        if target is None:
            msg = "Instance database allocation is missing"
            raise RuntimeError(msg)
        postgresql.create_schema(target)
        advanced = advance_execution(
            claim,
            next_task_name=INSTANCE_CREATE_STEP_02_TASK,
            next_step=INSTANCE_CREATE_STEP_02,
            session_factory=session_factory,
        )
        return {"status": "pending" if advanced else "noop"}

    return run_claimed_step(job_id, INSTANCE_CREATE_STEP_01_TASK, session_factory, operation)
