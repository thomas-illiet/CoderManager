"""Daily best-effort dispatch of direct Coder workspace stop builds."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.config import get_settings
from coder_manager.crypto import InstancePasswordCipher
from coder_manager.domains import coder
from coder_manager.models import Instance
from coder_manager.utils.instance_urls import InstancePublicUrlConfig

if TYPE_CHECKING:
    from pydantic import SecretStr
    from sqlalchemy.orm import Session, sessionmaker

    from coder_manager.config import Settings

logger = logging.getLogger(__name__)

DISPATCH_DAILY_WORKSPACE_STOPS_TASK = "coder_manager.dispatch_daily_workspace_stops"
STOP_INSTANCE_WORKSPACES_TASK = "coder_manager.stop_instance_workspaces"


@celery_app.task(name=DISPATCH_DAILY_WORKSPACE_STOPS_TASK)
def dispatch_daily_workspace_stops() -> dict[str, int | str]:
    """Dispatch one independent stop-submission task for every stored instance."""

    session_factory = worker_database.get_worker_session_maker()
    with session_factory() as session:
        instance_ids = tuple(session.scalars(select(Instance.id).order_by(Instance.id)))

    dispatched = 0
    failed = 0
    for instance_id in instance_ids:
        try:
            stop_instance_workspaces.delay(str(instance_id))
        except Exception:
            failed += 1
            logger.exception(
                "Could not dispatch daily workspace stops for instance %s",
                instance_id,
            )
            continue
        dispatched += 1

    if failed:
        msg = f"Could not dispatch daily workspace stops for {failed} instance(s)"
        raise RuntimeError(msg)
    return {"status": "success", "dispatched": dispatched}


@celery_app.task(name=STOP_INSTANCE_WORKSPACES_TASK)
def stop_instance_workspaces(instance_id: str) -> dict[str, int | str]:
    """Submit Coder stop builds for one instance without waiting or retrying."""

    settings = get_settings()
    url_config = InstancePublicUrlConfig.from_settings(settings)
    parsed_instance_id = UUID(instance_id)
    session_factory = worker_database.get_worker_session_maker()
    try:
        credentials = _stored_instance_credentials(
            parsed_instance_id,
            session_factory,
            settings,
            url_config,
        )
        if credentials is not None:
            instance_url, password = credentials
            result = coder.submit_active_workspace_stops(instance_url, password)
    except Exception:
        logger.exception(
            "Daily workspace stop submission failed for instance %s",
            parsed_instance_id,
        )
        raise
    if credentials is None:
        logger.info(
            "Skipping daily workspace stops because instance %s no longer exists",
            parsed_instance_id,
        )
        return {"status": "noop", "submitted": 0, "already_stopping": 0}
    logger.info(
        "Submitted %s daily workspace stop build(s) for instance %s; %s already stopping",
        len(result.submitted_ids),
        parsed_instance_id,
        len(result.already_stopping_ids),
    )
    return {
        "status": "success",
        "submitted": len(result.submitted_ids),
        "already_stopping": len(result.already_stopping_ids),
    }


def _stored_instance_credentials(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
    settings: Settings,
    url_config: InstancePublicUrlConfig,
) -> tuple[str, SecretStr] | None:
    """Read and decrypt one instance's administrator credentials without writing state."""

    with session_factory() as session:
        instance = session.get(Instance, instance_id)
        if instance is None:
            return None
        if instance.password_enc is None:
            msg = "Instance administrator password is missing"
            raise RuntimeError(msg)
        password = InstancePasswordCipher(settings.crypto_key).decrypt(
            instance.password_enc,
            instance.id,
        )
        return url_config.url_for(instance.slug, instance.environment), password
