"""Periodic observation of strict instance Application existence."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.domains import argocd
from coder_manager.models import Instance, InstanceState, InstanceStatus

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InstanceStateSnapshot:
    """Fields fencing one remote existence observation."""

    id: UUID
    slug: str
    attached_name: str | None
    environment: str
    job_id: UUID | None
    action: str
    status: InstanceStatus


@celery_app.task(name="coder_manager.check_instance_states")
def check_instance_states() -> dict[str, int]:
    """Observe idle Applications without triggering any remote mutation."""

    session_factory = worker_database.get_worker_session_maker()
    with session_factory() as session:
        instances = tuple(
            InstanceStateSnapshot(
                id=instance.id,
                slug=instance.slug,
                attached_name=instance.argocd_application_name,
                environment=instance.environment.value,
                job_id=instance.job_id,
                action=instance.action,
                status=instance.status,
            )
            for instance in session.scalars(
                select(Instance)
                .where(
                    Instance.status.not_in((InstanceStatus.PENDING, InstanceStatus.RUNNING)),
                    Instance.action != "deleting",
                )
                .order_by(Instance.id)
            )
        )

    checked = 0
    changed = 0
    errors = 0
    for snapshot in instances:
        try:
            exists = argocd.instance_application_exists(
                snapshot.slug,
                snapshot.attached_name,
                snapshot.environment,
            )
        except Exception:
            errors += 1
            logger.exception(
                "Could not observe Argo CD Application for instance %s",
                snapshot.id,
            )
            continue
        checked += 1
        observed = InstanceState.STARTED if exists else InstanceState.STOPPED
        with session_factory() as session:
            instance = session.scalar(
                select(Instance).where(Instance.id == snapshot.id).with_for_update()
            )
            if (
                instance is None
                or instance.job_id != snapshot.job_id
                or instance.action != snapshot.action
                or instance.status is not snapshot.status
                or instance.status in {InstanceStatus.PENDING, InstanceStatus.RUNNING}
                or instance.action == "deleting"
            ):
                continue
            if instance.state is not observed:
                instance.state = observed
                changed += 1
            session.commit()
    return {"checked": checked, "changed": changed, "errors": errors}
