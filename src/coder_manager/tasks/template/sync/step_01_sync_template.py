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
    TemplateTargetClaimLostError,
    fetch_template_archive,
    stable_assignment_ids,
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
        assignment_ids = stable_assignment_ids(template_id, session_factory)
        if not assignment_ids:
            completed = complete_execution(claim, session_factory)
            return {"status": "success" if completed else "noop"}
        snapshot = template_source_snapshot(template_id, session_factory)
        archive = fetch_template_archive(snapshot)
        failures = 0

        def heartbeat() -> None:
            """Keep the durable claim alive while Coder imports Terraform."""

            if not heartbeat_execution(claim, session_factory):
                message = "Template synchronization claim is no longer current"
                raise TemplateTargetClaimLostError(message)

        for assignment_id in assignment_ids:
            heartbeat()
            try:
                sync_template_target(
                    snapshot,
                    archive,
                    assignment_id,
                    session_factory,
                    claim=claim,
                    heartbeat=heartbeat,
                )
            except TemplateTargetClaimLostError:
                raise
            except Exception:
                failures += 1
                logger.exception(
                    "Template %s synchronization failed for assignment %s",
                    template_id,
                    assignment_id,
                )
        if failures:
            msg = f"Template synchronization failed for {failures} target(s)"
            raise RuntimeError(msg)
        completed = complete_execution(claim, session_factory)
        return {"status": "success" if completed else "noop"}

    return run_claimed_step(job_id, TEMPLATE_SYNC_STEP_01_TASK, session_factory, operation)
