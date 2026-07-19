"""Managed database synchronization task."""

from coder_manager.celery_app import celery_app
from coder_manager.tasks._common import JobResult, placeholder


@celery_app.task(name="coder_manager.sync_database")
def sync_database() -> JobResult:
    """Run the managed database synchronization placeholder."""

    placeholder()
    return {"status": "success"}
