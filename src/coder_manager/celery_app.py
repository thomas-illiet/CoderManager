"""Celery application configuration."""

from datetime import timedelta

from celery import Celery, bootsteps, signals
from celery.schedules import crontab

from coder_manager.config import get_settings
from coder_manager.metrics import CeleryMetrics, mark_worker_process_dead
from coder_manager.utils.instance_urls import InstancePublicUrlConfig
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
    timezone=settings.scheduler_timezone,
    enable_utc=True,
    task_track_started=True,
    beat_schedule={
        "retry-job-executions": {
            "task": "coder_manager.retry_job_executions",
            "schedule": timedelta(seconds=settings.job_retry_interval_seconds),
            "options": {"ignore_result": True},
        },
        "check-instance-states": {
            "task": "coder_manager.check_instance_states",
            "schedule": timedelta(hours=1),
            "options": {"ignore_result": True},
        },
        "dispatch-daily-workspace-stops": {
            "task": "coder_manager.dispatch_daily_workspace_stops",
            "schedule": crontab(minute=0, hour=0),
            "options": {"ignore_result": True},
        },
    },
)
celery_metrics = CeleryMetrics()


class ValidateInstancePublicUrlConfig(bootsteps.StartStopStep):
    """Stop worker startup when its public URL configuration is invalid."""

    def start(self, parent: object) -> None:
        """Validate settings before the worker accepts any task."""

        del parent
        InstancePublicUrlConfig.from_settings(settings)


worker_steps = celery_app.steps
if worker_steps is None:  # pragma: no cover - Celery initializes this registry eagerly
    msg = "Celery worker bootstep registry is unavailable"
    raise RuntimeError(msg)
worker_steps["worker"].add(ValidateInstancePublicUrlConfig)


def _registered_task_names() -> tuple[str, ...]:
    """Return the loaded Coder Manager task names in stable order."""

    return tuple(sorted(name for name in celery_app.tasks if name.startswith("coder_manager.")))


@signals.worker_init.connect
def initialize_worker_metrics(**_kwargs: object) -> None:
    """Initialize worker metrics before pool children fork."""

    celery_metrics.initialize_component("worker", _registered_task_names())


@signals.worker_ready.connect
def start_worker_metrics_server(**_kwargs: object) -> None:
    """Expose aggregated worker metrics once the worker accepts tasks."""

    celery_metrics.initialize_component("worker", _registered_task_names())
    celery_metrics.start_server(settings.metrics_host, settings.metrics_port)


@signals.worker_shutdown.connect
def shutdown_worker_metrics_server(**_kwargs: object) -> None:
    """Stop the worker metrics listener during a clean shutdown."""

    celery_metrics.stop_server()


@signals.beat_init.connect
def initialize_beat_metrics(**_kwargs: object) -> None:
    """Initialize and expose Beat publication metrics."""

    celery_metrics.initialize_component("beat")
    celery_metrics.start_server(settings.metrics_host, settings.metrics_port)


@signals.after_task_publish.connect
def record_beat_task_publication(sender: str | None = None, **_kwargs: object) -> None:
    """Count successful task publications originating from Beat."""

    if sender is not None:
        celery_metrics.task_published(sender)


@signals.task_prerun.connect
def record_worker_task_start(
    task_id: str | None = None,
    task: object | None = None,
    **_kwargs: object,
) -> None:
    """Record the start time and active gauge for one worker task."""

    task_name = getattr(task, "name", None)
    if task_id is not None and isinstance(task_name, str):
        celery_metrics.task_started(task_id, task_name)


@signals.task_postrun.connect
def record_worker_task_finish(
    task_id: str | None = None,
    task: object | None = None,
    state: str | None = None,
    **_kwargs: object,
) -> None:
    """Record one terminal state and duration for a worker task."""

    task_name = getattr(task, "name", None)
    if task_id is not None and isinstance(task_name, str):
        celery_metrics.task_finished(task_id, task_name, state)


@signals.worker_process_init.connect
def initialize_worker_process_database(**_kwargs: object) -> None:
    """Create the synchronous DB engine after the Celery pool process starts."""

    initialize_worker_database()


@signals.worker_process_shutdown.connect
def shutdown_worker_process_database(pid: int | None = None, **_kwargs: object) -> None:
    """Dispose the synchronous DB engine before the Celery pool process exits."""

    shutdown_worker_database()
    if pid is not None:
        mark_worker_process_dead(pid)
