"""Remove the remote Argo CD Application for a deleting instance."""

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.domains import argocd
from coder_manager.models import Instance
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    advance_execution,
    required_resource_id,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import (
    INSTANCE_DELETE_STEP_02_TASK,
    INSTANCE_DELETE_STEP_03,
    INSTANCE_DELETE_STEP_03_TASK,
)


@celery_app.task(name=INSTANCE_DELETE_STEP_02_TASK)
def step_02_remove_instance(job_id: str) -> dict[str, str]:
    """Delete the remote instance idempotently and schedule schema removal."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Delete Argo CD state and advance the durable job."""

        with session_factory() as session:
            instance = session.get(Instance, claim.resource_id)
            if instance is None:
                msg = "Instance is missing"
                raise RuntimeError(msg)
            attached_name = instance.argocd_application_name
        argocd.delete_instance_application(required_resource_id(claim), attached_name)
        advanced = advance_execution(
            claim,
            next_task_name=INSTANCE_DELETE_STEP_03_TASK,
            next_step=INSTANCE_DELETE_STEP_03,
            session_factory=session_factory,
        )
        return {"status": "pending" if advanced else "noop"}

    return run_claimed_step(job_id, INSTANCE_DELETE_STEP_02_TASK, session_factory, operation)
