"""Synchronize compatible templates before declaring a new instance ready."""

import logging

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    complete_execution,
    heartbeat_execution,
    required_resource_id,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import INSTANCE_CREATE_STEP_04_TASK
from coder_manager.tasks.template._sync import (
    compatible_template_ids,
    fetch_template_archive,
    sync_template_target,
    template_source_snapshot,
)

logger = logging.getLogger(__name__)


@celery_app.task(name=INSTANCE_CREATE_STEP_04_TASK)
def step_04_sync_templates(job_id: str) -> dict[str, str]:
    """Converge every compatible current branch before instance readiness."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Synchronize global and application templates for the new instance."""

        instance_id = required_resource_id(claim)

        def heartbeat() -> None:
            """Keep the instance creation claim alive during remote imports."""

            heartbeat_execution(claim, session_factory)

        for template_id in compatible_template_ids(instance_id, session_factory):
            heartbeat()
            snapshot = template_source_snapshot(template_id, session_factory)
            archive = fetch_template_archive(snapshot)
            try:
                sync_template_target(
                    snapshot,
                    archive,
                    instance_id,
                    session_factory,
                    heartbeat=heartbeat,
                )
            except Exception:
                logger.exception(
                    "Template %s synchronization failed during instance %s bootstrap",
                    template_id,
                    instance_id,
                )
                raise
        completed = complete_execution(claim, session_factory)
        return {"status": "success" if completed else "noop"}

    return run_claimed_step(job_id, INSTANCE_CREATE_STEP_04_TASK, session_factory, operation)
