"""Shared lifecycle implementation for workspace tasks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from coder_manager.models import Workspace, WorkspaceStatus
from coder_manager.tasks import _common

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session, sessionmaker

    from coder_manager.tasks._common import JobResult


def _workspace_lifecycle(
    workspace_id: UUID,
    *,
    expected_action: str,
    delete_on_success: bool,
    session_factory: sessionmaker[Session],
) -> JobResult:
    """Transition one workspace and optionally delete it after provisioning."""

    # Claim the pending operation before running work outside the transaction.
    with session_factory() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.id == workspace_id).with_for_update()
        )
        if (
            workspace is None
            or workspace.action != expected_action
            or workspace.status is not WorkspaceStatus.PENDING
        ):
            return {"status": "noop"}
        workspace.status = WorkspaceStatus.RUNNING
        session.commit()

    # Execute the external lifecycle operation without holding database locks.
    try:
        _common.placeholder()
    except Exception:
        _mark_workspace_error(workspace_id, expected_action, session_factory)
        raise

    # Finalize only if no newer action superseded the claimed operation.
    with session_factory() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.id == workspace_id).with_for_update()
        )
        if (
            workspace is None
            or workspace.action != expected_action
            or workspace.status is not WorkspaceStatus.RUNNING
        ):
            return {"status": "noop"}
        if delete_on_success:
            session.delete(workspace)
            result = {"status": "deleted"}
        else:
            workspace.status = WorkspaceStatus.SUCCESS
            result = {"status": "success"}
        session.commit()
        return result


def _mark_workspace_error(
    workspace_id: UUID,
    expected_action: str,
    session_factory: sessionmaker[Session],
) -> None:
    """Mark a still-current running workspace operation as failed."""

    with session_factory() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.id == workspace_id).with_for_update()
        )
        if (
            workspace is not None
            and workspace.action == expected_action
            and workspace.status is WorkspaceStatus.RUNNING
        ):
            workspace.status = WorkspaceStatus.ERROR
            session.commit()
