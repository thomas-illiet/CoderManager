"""Celery worker healthcheck task."""

from typing import Any

from coder_manager.celery_app import celery_app


@celery_app.task(name="coder_manager.healthcheck")
def healthcheck() -> dict[str, Any]:
    """Verify worker and result-backend wiring."""

    return {"status": "ok"}
