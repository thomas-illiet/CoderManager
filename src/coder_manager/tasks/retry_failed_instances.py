"""Periodic recovery for failed instance lifecycle jobs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import select

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.models import Instance, InstanceStatus
from coder_manager.tasks.delete_instance import delete_instance
from coder_manager.tasks.upsert_instance import UPSERT_ACTIONS, upsert_instance

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


class RetryResult(TypedDict):
    """Human-readable summary returned by one recovery scan."""

    status: str
    scheduled: int
    skipped: int


@celery_app.task(name="coder_manager.retry_failed_instances")
def retry_failed_instances() -> RetryResult:
    """Schedule one safe retry for every failed instance lifecycle operation."""

    return _retry_failed_instances(worker_database.get_worker_session_maker())


def _retry_failed_instances(
    session_factory: sessionmaker[Session],
    upsert_dispatch: Callable[..., object] | None = None,
    delete_dispatch: Callable[..., object] | None = None,
) -> RetryResult:
    """Read failed rows without changing them, then dispatch shallow retry jobs."""

    with session_factory() as session:
        failed_instances = list(
            session.execute(
                select(Instance.id, Instance.action)
                .where(Instance.status == InstanceStatus.ERROR)
                .order_by(Instance.id)
            ).tuples()
        )

    dispatch_upsert = upsert_dispatch or upsert_instance.delay
    dispatch_delete = delete_dispatch or delete_instance.delay
    scheduled = 0
    skipped = 0
    for instance_id, action in failed_instances:
        try:
            if action in UPSERT_ACTIONS:
                dispatch_upsert(str(instance_id), retry_error=True)
            elif action == "deleting":
                dispatch_delete(str(instance_id), retry_error=True)
            else:
                logger.warning(
                    "Skipping failed instance %s with unsupported action %s",
                    instance_id,
                    action,
                )
                skipped += 1
                continue
        except Exception:
            # The row remains in error so the next Beat pass can try again.
            logger.exception("Could not schedule retry for failed instance %s", instance_id)
            skipped += 1
        else:
            scheduled += 1

    return {"status": "success", "scheduled": scheduled, "skipped": skipped}
