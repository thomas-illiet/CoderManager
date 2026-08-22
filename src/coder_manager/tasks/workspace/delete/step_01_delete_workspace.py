"""Delete one remote workspace before removing local configuration."""

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.config import get_settings
from coder_manager.domains.coder import CoderClient, CoderWorkspaceBuild
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    heartbeat_execution,
    required_resource_id,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import WORKSPACE_DELETE_STEP_01_TASK
from coder_manager.tasks.workspace._remote import (
    complete_workspace,
    find_remote_workspace,
    store_remote_ids,
    wait_workspace_build,
    workspace_remote_snapshot,
)
from coder_manager.utils.instance_urls import InstancePublicUrlConfig


@celery_app.task(name=WORKSPACE_DELETE_STEP_01_TASK)
def step_01_delete_workspace(job_id: str) -> dict[str, str]:
    """Run workspace deletion and remove its local configuration."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Delete the remote workspace idempotently and remove its local row."""

        settings = get_settings()
        url_config = InstancePublicUrlConfig.from_settings(settings)
        snapshot = workspace_remote_snapshot(
            required_resource_id(claim),
            session_factory,
            url_config,
        )

        def heartbeat() -> None:
            """Keep the durable claim alive during remote deletion."""

            heartbeat_execution(claim, session_factory)

        with CoderClient(snapshot.instance_url) as client:
            client.authenticate_prepared_admin(snapshot.password)
            remote = find_remote_workspace(client, snapshot)
            if remote is None:
                completed = complete_workspace(
                    claim,
                    session_factory,
                    delete_resource=True,
                )
                return {"status": "deleted" if completed else "noop"}
            build = CoderWorkspaceBuild(
                id=remote.latest_build_id,
                status=remote.status,
                transition=remote.latest_build_transition,
            )
            if build.transition != "delete" or build.status in {
                "failed",
                "canceled",
                "canceling",
            }:
                build = client.create_workspace_delete_build(remote.id)
            if not store_remote_ids(
                claim,
                session_factory,
                workspace_id=remote.id,
                build_id=build.id,
            ):
                return {"status": "noop"}
            wait_workspace_build(
                client,
                build,
                success_status="deleted",
                timeout_seconds=settings.workspace_build_timeout_seconds,
                poll_interval_seconds=settings.workspace_build_poll_interval_seconds,
                heartbeat=heartbeat,
            )
        completed = complete_workspace(
            claim,
            session_factory,
            delete_resource=True,
        )
        return {"status": "deleted" if completed else "noop"}

    return run_claimed_step(job_id, WORKSPACE_DELETE_STEP_01_TASK, session_factory, operation)
