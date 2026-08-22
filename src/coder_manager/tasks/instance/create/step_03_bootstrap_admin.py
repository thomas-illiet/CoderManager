"""Bootstrap the static administrator account on a managed Coder instance."""

from secrets import token_urlsafe

from pydantic import SecretStr

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.config import get_settings
from coder_manager.domains import coder
from coder_manager.models import Instance, JobExecution
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    advance_execution,
    required_resource_id,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import (
    INSTANCE_CREATE_STEP_03_TASK,
    INSTANCE_CREATE_STEP_04,
    INSTANCE_CREATE_STEP_04_TASK,
    INSTANCE_UPDATE_STEP_02,
    INSTANCE_UPDATE_STEP_02_TASK,
)
from coder_manager.tasks.instance._bootstrap import store_verified_admin_password
from coder_manager.utils.instance_urls import InstancePublicUrlConfig


@celery_app.task(name=INSTANCE_CREATE_STEP_03_TASK)
def step_03_bootstrap_admin(job_id: str) -> dict[str, str]:
    """Create or recover the first Coder administrator, then complete the job."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Bootstrap only missing credentials and persist them after remote success."""

        settings = get_settings()
        url_config = InstancePublicUrlConfig.from_settings(settings)
        instance_id = required_resource_id(claim)
        with session_factory() as session:
            instance = session.get(Instance, instance_id)
            job = session.get(JobExecution, claim.job_id)
            if instance is None:
                msg = "Instance is missing"
                raise RuntimeError(msg)
            if job is None:
                msg = "Job execution is missing"
                raise RuntimeError(msg)
            password_configured = instance.password_enc is not None
            instance_url = url_config.url_for(instance.slug, instance.environment)
            is_update = job.name == "instance.update"

        password: SecretStr | None = None
        if not password_configured:
            password = SecretStr(token_urlsafe(32))
            coder.bootstrap_admin_account(instance_url, password)

        def store_password(_session: object, resource: object | None) -> None:
            """Store the verified password in the same transaction as advancement."""

            if not isinstance(resource, Instance):
                msg = "Instance is missing"
                raise TypeError(msg)
            if password is not None:
                store_verified_admin_password(resource, password)

        advanced = advance_execution(
            claim,
            next_task_name=(
                INSTANCE_UPDATE_STEP_02_TASK if is_update else INSTANCE_CREATE_STEP_04_TASK
            ),
            next_step=INSTANCE_UPDATE_STEP_02 if is_update else INSTANCE_CREATE_STEP_04,
            session_factory=session_factory,
            mutate=store_password,
        )
        return {"status": "pending" if advanced else "noop"}

    return run_claimed_step(
        job_id,
        INSTANCE_CREATE_STEP_03_TASK,
        session_factory,
        operation,
    )
