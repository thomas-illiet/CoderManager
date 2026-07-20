"""Shared types and failure handling for lifecycle tasks."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from celery import Task
from sqlalchemy import select

from coder_manager import worker_database
from coder_manager.models import (
    Instance,
    InstanceStatus,
    Member,
    MemberStatus,
    Workspace,
    WorkspaceStatus,
)

if TYPE_CHECKING:
    from billiard.einfo import ExceptionInfo
    from sqlalchemy.orm import Session, sessionmaker

JobResult = dict[str, str]

logger = logging.getLogger(__name__)


class StatefulResourceTask(Task):
    """Celery task that fails its still-active database transition on errors."""

    abstract = True
    resource_type: str
    expected_action: str
    fail_running_members = False

    def on_failure(
        self,
        exc: BaseException,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        einfo: ExceptionInfo,
    ) -> None:
        """Turn a matching pending/running transition into an error state."""

        try:
            resource_id = UUID(str(args[0]))
            session_factory = worker_database.get_worker_session_maker()
            if self.resource_type == "instance":
                _fail_instance_transition(
                    resource_id,
                    self.expected_action,
                    session_factory,
                    fail_running_members=self.fail_running_members,
                )
            elif self.resource_type == "workspace":
                _fail_workspace_transition(resource_id, self.expected_action, session_factory)
            else:  # pragma: no cover - task registration invariant
                logger.error("Unsupported stateful resource type: %s", self.resource_type)
        except Exception:
            # Failure bookkeeping must never replace Celery's original task error.
            logger.exception(
                "Could not persist the failure state for task %s (%s)",
                self.name,
                task_id,
            )
        return super().on_failure(exc, task_id, args, kwargs, einfo)


def _fail_instance_transition(
    instance_id: UUID,
    expected_action: str,
    session_factory: sessionmaker[Session],
    *,
    fail_running_members: bool,
) -> None:
    """Fail only the instance transition still owned by the crashed job."""

    with session_factory() as session:
        instance = session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if (
            instance is None
            or instance.action != expected_action
            or instance.status not in {InstanceStatus.PENDING, InstanceStatus.RUNNING}
        ):
            return
        instance.status = InstanceStatus.ERROR
        if fail_running_members:
            running_members = session.scalars(
                select(Member).where(
                    Member.instance_id == instance_id,
                    Member.status == MemberStatus.RUNNING,
                )
            )
            for member in running_members:
                member.status = MemberStatus.ERROR
        session.commit()


def _fail_workspace_transition(
    workspace_id: UUID,
    expected_action: str,
    session_factory: sessionmaker[Session],
) -> None:
    """Fail only the workspace transition still owned by the crashed job."""

    with session_factory() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.id == workspace_id).with_for_update()
        )
        if (
            workspace is None
            or workspace.action != expected_action
            or workspace.status not in {WorkspaceStatus.PENDING, WorkspaceStatus.RUNNING}
        ):
            return
        workspace.status = WorkspaceStatus.ERROR
        session.commit()


def placeholder() -> None:
    """Stand in for a future call to the Coder provisioning backend."""
