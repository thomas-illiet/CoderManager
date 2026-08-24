"""Bootstrap the static administrator account on a managed Coder instance."""

from dataclasses import dataclass
from secrets import token_urlsafe

from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.config import Settings, get_settings
from coder_manager.crypto import InstancePasswordCipher
from coder_manager.domains import coder
from coder_manager.models import Instance
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    advance_execution,
    owned_execution,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import (
    INSTANCE_CREATE_STEP_03_TASK,
    INSTANCE_CREATE_STEP_04,
    INSTANCE_CREATE_STEP_04_TASK,
    INSTANCE_UPDATE_STEP_02,
    INSTANCE_UPDATE_STEP_02_TASK,
)
from coder_manager.utils.instance_urls import InstancePublicUrlConfig


@dataclass(frozen=True, slots=True)
class _BootstrapPreparation:
    """Durable input required for one remote bootstrap attempt."""

    instance_url: str
    is_update: bool
    password: SecretStr | None


def _prepare_bootstrap(
    claim: ExecutionClaim,
    session_factory: sessionmaker[Session],
    settings: Settings,
    url_config: InstancePublicUrlConfig,
) -> _BootstrapPreparation | None:
    """Commit or reload one encrypted candidate before any remote request."""

    with session_factory() as session:
        owned = owned_execution(session, claim)
        if owned is None:
            return None
        job, resource = owned
        if not isinstance(resource, Instance):
            msg = "Instance is missing"
            raise TypeError(msg)
        password: SecretStr | None = None
        if resource.password_enc is None:
            cipher = InstancePasswordCipher(settings.crypto_key)
            if resource.password_candidate_enc is None:
                password = SecretStr(token_urlsafe(32))
                resource.password_candidate_enc = cipher.encrypt(password, resource.id)
                session.commit()
            else:
                password = cipher.decrypt(resource.password_candidate_enc, resource.id)
        return _BootstrapPreparation(
            instance_url=url_config.url_for(resource.slug, resource.environment),
            is_update=job.name == "instance.update",
            password=password,
        )


def _promote_password(_session: Session, resource: object | None) -> None:
    """Promote only the verified candidate in the advancement transaction."""

    if not isinstance(resource, Instance):
        msg = "Instance is missing"
        raise TypeError(msg)
    if resource.password_enc is None:
        if resource.password_candidate_enc is None:
            msg = "Instance administrator password candidate is missing"
            raise RuntimeError(msg)
        resource.password_enc = resource.password_candidate_enc
    resource.password_candidate_enc = None


@celery_app.task(name=INSTANCE_CREATE_STEP_03_TASK)
def step_03_bootstrap_admin(job_id: str) -> dict[str, str]:
    """Create or recover the first Coder administrator, then complete the job."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Persist a reusable candidate before bootstrap, then promote it after success."""

        settings = get_settings()
        url_config = InstancePublicUrlConfig.from_settings(settings)
        preparation = _prepare_bootstrap(claim, session_factory, settings, url_config)
        if preparation is None:
            return {"status": "noop"}
        if preparation.password is not None:
            coder.bootstrap_admin_account(preparation.instance_url, preparation.password)

        advanced = advance_execution(
            claim,
            next_task_name=(
                INSTANCE_UPDATE_STEP_02_TASK
                if preparation.is_update
                else INSTANCE_CREATE_STEP_04_TASK
            ),
            next_step=(
                INSTANCE_UPDATE_STEP_02 if preparation.is_update else INSTANCE_CREATE_STEP_04
            ),
            session_factory=session_factory,
            mutate=_promote_password,
        )
        return {"status": "pending" if advanced else "noop"}

    return run_claimed_step(
        job_id,
        INSTANCE_CREATE_STEP_03_TASK,
        session_factory,
        operation,
    )
