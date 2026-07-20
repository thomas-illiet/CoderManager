"""Remove all local configuration for a deleting instance."""

from sqlalchemy import delete

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.models import (
    DatabaseAllocation,
    Instance,
    InstanceKubernetes,
    JobStatus,
    Member,
    Workspace,
)
from coder_manager.tasks.common.execution import ExecutionClaim, owned_execution, run_claimed_step
from coder_manager.tasks.common.registry import INSTANCE_DELETE_STEP_04_TASK


@celery_app.task(name=INSTANCE_DELETE_STEP_04_TASK)
def step_04_remove_local_configuration(job_id: str) -> dict[str, str]:
    """Delete local dependents and the instance in one final transaction."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Delete every local row owned by the instance."""

        with session_factory() as session:
            owned = owned_execution(session, claim)
            if owned is None:
                return {"status": "noop"}
            job, instance = owned
            if not isinstance(instance, Instance):
                return {"status": "noop"}
            session.execute(delete(Workspace).where(Workspace.instance_id == instance.id))
            session.execute(delete(Member).where(Member.instance_id == instance.id))
            session.execute(
                delete(DatabaseAllocation).where(DatabaseAllocation.instance_id == instance.id)
            )
            session.execute(
                delete(InstanceKubernetes).where(InstanceKubernetes.instance_id == instance.id)
            )
            job.status = JobStatus.SUCCESS
            job.claimed_at = None
            session.delete(instance)
            session.commit()
            return {"status": "deleted"}

    return run_claimed_step(job_id, INSTANCE_DELETE_STEP_04_TASK, session_factory, operation)
