"""Delete an instance Application after every workspace is stopped."""

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.domains import argocd
from coder_manager.models import Instance, InstanceState
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    complete_execution,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import INSTANCE_STOP_STEP_02_TASK


@celery_app.task(name=INSTANCE_STOP_STEP_02_TASK)
def step_02_stop_instance(job_id: str) -> dict[str, str]:
    """Delete only the remote Application and preserve all local resources."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Delete the Application idempotently and record the observed state."""

        with session_factory() as session:
            instance = session.get(Instance, claim.resource_id)
            if instance is None:
                msg = "Instance is missing"
                raise RuntimeError(msg)
            slug = instance.slug
            attached_name = instance.argocd_application_name

        argocd.delete_instance_application(slug, attached_name)

        def mark_stopped(_session: object, resource: object | None) -> None:
            """Persist stopped only after Argo confirms deletion."""

            if not isinstance(resource, Instance):
                msg = "Instance is missing"
                raise TypeError(msg)
            resource.state = InstanceState.STOPPED

        completed = complete_execution(
            claim,
            session_factory,
            mutate=mark_stopped,
        )
        return {"status": "success" if completed else "noop"}

    return run_claimed_step(job_id, INSTANCE_STOP_STEP_02_TASK, session_factory, operation)
