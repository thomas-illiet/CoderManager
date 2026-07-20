"""Celery application configuration."""

from datetime import timedelta

from celery import Celery, signals

from coder_manager.config import get_settings
from coder_manager.worker_database import initialize_worker_database, shutdown_worker_database

settings = get_settings()
celery_app = Celery(
    "coder_manager",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["coder_manager.tasks"],
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    beat_schedule={
        "retry-job-executions": {
            "task": "coder_manager.retry_job_executions",
            "schedule": timedelta(seconds=settings.job_retry_interval_seconds),
        }
    },
)


@signals.worker_process_init.connect
def initialize_worker_process_database(**_kwargs: object) -> None:
    """Create the synchronous DB engine after the Celery pool process starts."""

    initialize_worker_database()


@signals.worker_process_shutdown.connect
def shutdown_worker_process_database(**_kwargs: object) -> None:
    """Dispose the synchronous DB engine before the Celery pool process exits."""

    shutdown_worker_database()
