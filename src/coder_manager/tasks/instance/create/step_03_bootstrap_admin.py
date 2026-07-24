"""Bootstrap the static administrator account on a managed Coder instance."""

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.domains import coder
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    advance_execution,
    required_resource_id,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import (
    INSTANCE_CREATE_STEP_03_TASK,
    INSTANCE_CREATE_STEP_04,
    INSTANCE_CREATE_STEP_04_TASK,
)
from coder_manager.tasks.instance._bootstrap import (
    bootstrap_succeeded,
    prepared_admin_password,
)


@celery_app.task(name=INSTANCE_CREATE_STEP_03_TASK)
def step_03_bootstrap_admin(job_id: str) -> dict[str, str]:
    """Create or recover the first Coder administrator, then complete the job."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Reuse prepared credentials and finish only after remote verification."""

        instance_id = required_resource_id(claim)
        with session_factory() as session:
            already_succeeded = bootstrap_succeeded(session, instance_id)
        if already_succeeded:
            advanced = advance_execution(
                claim,
                next_task_name=INSTANCE_CREATE_STEP_04_TASK,
                next_step=INSTANCE_CREATE_STEP_04,
                session_factory=session_factory,
            )
            return {"status": "pending" if advanced else "noop"}

        instance_url, password = prepared_admin_password(instance_id, session_factory)
        coder.bootstrap_admin_account(instance_url, password)
        advanced = advance_execution(
            claim,
            next_task_name=INSTANCE_CREATE_STEP_04_TASK,
            next_step=INSTANCE_CREATE_STEP_04,
            session_factory=session_factory,
        )
        return {"status": "pending" if advanced else "noop"}

    return run_claimed_step(
        job_id,
        INSTANCE_CREATE_STEP_03_TASK,
        session_factory,
        operation,
    )
