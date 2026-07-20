"""Coder workspace creation task."""

from uuid import UUID

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.tasks._common import JobResult, StatefulResourceTask
from coder_manager.tasks._workspace_lifecycle import _workspace_lifecycle


@celery_app.task(
    name="coder_manager.create_workspace",
    base=StatefulResourceTask,
    resource_type="workspace",
    expected_action="creating",
)
def create_workspace(workspace_id: str) -> JobResult:
    """Run the placeholder creation lifecycle for one Coder workspace."""

    return _workspace_lifecycle(
        UUID(workspace_id),
        expected_action="creating",
        delete_on_success=False,
        session_factory=worker_database.get_worker_session_maker(),
    )
