"""Create one remote workspace and wait for its first build."""

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
from coder_manager.tasks.common.registry import WORKSPACE_CREATE_STEP_01_TASK
from coder_manager.tasks.workspace._remote import (
    complete_workspace,
    find_remote_workspace,
    require_matching_template,
    store_remote_ids,
    wait_workspace_build,
    workspace_remote_snapshot,
)
from coder_manager.utils.instance_urls import InstancePublicUrlConfig


@celery_app.task(name=WORKSPACE_CREATE_STEP_01_TASK)
def step_01_create_workspace(job_id: str) -> dict[str, str]:
    """Run workspace creation and finalize its local state."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Create or adopt the workspace and finalize only after a running build."""

        settings = get_settings()
        url_config = InstancePublicUrlConfig.from_settings(settings)
        snapshot = workspace_remote_snapshot(
            required_resource_id(claim),
            session_factory,
            url_config,
        )

        def heartbeat() -> None:
            """Keep the durable claim alive during remote provisioning."""

            heartbeat_execution(claim, session_factory)

        with CoderClient(snapshot.instance_url) as client:
            client.authenticate_prepared_admin(snapshot.password)
            remote = find_remote_workspace(client, snapshot)
            created = remote is None
            if remote is None:
                remote = client.create_workspace(
                    snapshot.username,
                    name=snapshot.name,
                    template_id=snapshot.coder_template_id,
                    rich_parameter_values=snapshot.parameters,
                )
            require_matching_template(remote, snapshot)
            build = CoderWorkspaceBuild(
                id=remote.latest_build_id,
                status=remote.status,
                transition=remote.latest_build_transition,
            )
            if not created:
                current_parameters = (
                    client.workspace_build_parameters(build.id)
                    if build.transition == "start"
                    else ()
                )
                if current_parameters != snapshot.parameters or build.status in {
                    "stopped",
                    "failed",
                    "canceled",
                    "canceling",
                    "deleting",
                    "deleted",
                }:
                    build = client.create_workspace_start_build(
                        remote.id,
                        snapshot.parameters,
                    )
            if not store_remote_ids(
                claim,
                session_factory,
                workspace_id=remote.id,
                build_id=build.id,
            ):
                return {"status": "noop"}
            build = wait_workspace_build(
                client,
                build,
                success_status="running",
                timeout_seconds=settings.workspace_build_timeout_seconds,
                poll_interval_seconds=settings.workspace_build_poll_interval_seconds,
                heartbeat=heartbeat,
            )
        completed = complete_workspace(
            claim,
            session_factory,
            workspace_id=remote.id,
            build_id=build.id,
            apply_parameters=True,
        )
        return {"status": "success" if completed else "noop"}

    return run_claimed_step(job_id, WORKSPACE_CREATE_STEP_01_TASK, session_factory, operation)
