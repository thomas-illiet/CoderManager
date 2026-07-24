"""Synchronize one template branch to every compatible ready Coder instance."""

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
from coder_manager.tasks.common.registry import TEMPLATE_SYNC_STEP_01_TASK
from coder_manager.tasks.template._sync import (
    fetch_template_archive,
    ready_instance_ids,
    sync_template_target,
    template_source_snapshot,
)

logger = logging.getLogger(__name__)


@celery_app.task(name=TEMPLATE_SYNC_STEP_01_TASK)
def step_01_sync_template(job_id: str) -> dict[str, str]:
    """Fetch the branch once, then converge every compatible ready target."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Synchronize all current targets while preserving partial successes."""

        template_id = required_resource_id(claim)
        snapshot = template_source_snapshot(template_id, session_factory)
        archive = fetch_template_archive(snapshot)
        failures = 0

        def heartbeat() -> None:
            """Keep the durable claim alive while Coder imports Terraform."""

            heartbeat_execution(claim, session_factory)

        for instance_id in ready_instance_ids(template_id, session_factory):
            heartbeat()
            try:
                sync_template_target(
                    snapshot,
                    archive,
                    instance_id,
                    session_factory,
                    heartbeat=heartbeat,
                )
            except Exception:
                failures += 1
                logger.exception(
                    "Template %s synchronization failed for instance %s",
                    template_id,
                    instance_id,
                )
        if failures:
            msg = f"Template synchronization failed for {failures} target(s)"
            raise RuntimeError(msg)
        completed = complete_execution(claim, session_factory)
        return {"status": "success" if completed else "noop"}

    return run_claimed_step(job_id, TEMPLATE_SYNC_STEP_01_TASK, session_factory, operation)
