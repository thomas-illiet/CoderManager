"""Publish one explicitly assigned template to its Coder instance."""

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.models import TemplateAssignment
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    complete_execution,
    heartbeat_execution,
    required_resource_id,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK
from coder_manager.tasks.template._sync import (
    TemplateTargetClaimLostError,
    assignment_template_id,
    fetch_template_archive,
    sync_template_target,
    template_source_snapshot,
)


def _mark_created(_session: object, resource: object | None) -> None:
    """Persist the stable assignment action after remote publication succeeds."""

    if not isinstance(resource, TemplateAssignment):
        msg = "Template assignment is missing"
        raise TypeError(msg)
    resource.action = "created"


@celery_app.task(name=TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK)
def step_01_create_template(job_id: str) -> dict[str, str]:
    """Publish one assignment and expose it only after successful convergence."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Fetch once and synchronize the assignment under its durable claim."""

        assignment_id = required_resource_id(claim)
        template_id = assignment_template_id(assignment_id, session_factory)
        snapshot = template_source_snapshot(template_id, session_factory)
        archive = fetch_template_archive(snapshot)

        def heartbeat() -> None:
            """Keep the assignment claim alive while Coder imports Terraform."""

            if not heartbeat_execution(claim, session_factory):
                message = "Template synchronization claim is no longer current"
                raise TemplateTargetClaimLostError(message)

        sync_template_target(
            snapshot,
            archive,
            assignment_id,
            session_factory,
            claim=claim,
            heartbeat=heartbeat,
        )
        completed = complete_execution(
            claim,
            session_factory,
            mutate=_mark_created,
        )
        return {"status": "success" if completed else "noop"}

    return run_claimed_step(
        job_id,
        TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
        session_factory,
        operation,
    )
