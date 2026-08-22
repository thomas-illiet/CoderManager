"""Update one remote workspace and apply mutable parameters."""

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
from coder_manager.tasks.common.registry import WORKSPACE_UPDATE_STEP_01_TASK
from coder_manager.tasks.workspace._remote import (
    WorkspaceRemoteError,
    complete_workspace,
    find_remote_workspace,
    require_matching_template,
    store_remote_ids,
    wait_workspace_build,
    workspace_remote_snapshot,
)
from coder_manager.utils.instance_urls import InstancePublicUrlConfig


@celery_app.task(name=WORKSPACE_UPDATE_STEP_01_TASK)
def step_01_update_workspace(job_id: str) -> dict[str, str]:
    """Run workspace update and finalize its local state."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Rename and rebuild the workspace when parameter state changed."""

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
            if remote is None:
                msg = "Remote workspace is missing"
                raise WorkspaceRemoteError(msg)
            require_matching_template(remote, snapshot)
            if remote.name != snapshot.name:
                client.update_workspace_name(remote.id, snapshot.name)

            build: CoderWorkspaceBuild | None = None
            if snapshot.applied_parameters_revision != snapshot.parameters_revision:
                latest = CoderWorkspaceBuild(
                    id=remote.latest_build_id,
                    status=remote.status,
                    transition=remote.latest_build_transition,
                )
                if (
                    latest.transition == "start"
                    and latest.status in {"pending", "starting", "running"}
                    and client.workspace_build_parameters(latest.id) == snapshot.parameters
                ):
                    build = latest
                else:
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
            build_id=build.id if build is not None else None,
            apply_parameters=build is not None,
        )
        return {"status": "success" if completed else "noop"}

    return run_claimed_step(job_id, WORKSPACE_UPDATE_STEP_01_TASK, session_factory, operation)
