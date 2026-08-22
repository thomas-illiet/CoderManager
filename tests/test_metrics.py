"""Prometheus API and Celery metrics tests."""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import generate_latest

from coder_manager import celery_app
from coder_manager.config import Settings
from coder_manager.metrics import (
    ApiMetrics,
    ApiMetricsMiddleware,
    CeleryMetrics,
    prepare_multiprocess_directory,
    start_metrics_server,
)

if TYPE_CHECKING:
    from pathlib import Path


def metric_text(metrics: ApiMetrics | CeleryMetrics) -> str:
    """Render one metrics owner as Prometheus text for assertions."""

    registry = (
        metrics.registry if isinstance(metrics, ApiMetrics) else metrics.exposition_registry()
    )
    return generate_latest(registry).decode()


def test_metrics_server_exposes_metrics_and_health_paths() -> None:
    """Serve Prometheus output and liveness while rejecting every other path."""

    metrics = ApiMetrics()
    server = start_metrics_server("127.0.0.1", 0, metrics.registry)
    try:
        with urlopen(f"http://127.0.0.1:{server.port}/metrics", timeout=2) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/plain")
            assert b"coder_manager_api_up 1.0" in response.read()

        with urlopen(f"http://127.0.0.1:{server.port}/health", timeout=2) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "application/json"
            assert response.read() == b'{"status":"ok"}'

        with pytest.raises(HTTPError) as missing:
            urlopen(f"http://127.0.0.1:{server.port}/unknown", timeout=2)
        assert missing.value.code == 404
    finally:
        server.stop()


async def test_api_metrics_normalize_routes_and_capture_errors() -> None:
    """Record route templates, fixed unmatched labels, statuses, and durations."""

    metrics = ApiMetrics()
    application = FastAPI()
    application.add_middleware(ApiMetricsMiddleware, metrics=metrics)

    @application.get("/items/{item_id}")
    async def get_item(item_id: str) -> dict[str, str]:
        """Return one item for the instrumented success scenario."""

        return {"id": item_id}

    @application.get("/failure")
    async def fail() -> None:
        """Raise an unhandled error for the instrumented failure scenario."""

        msg = "test failure"
        raise RuntimeError(msg)

    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/items/3f5acb75-fd25-4d5f-b522-a9b8085529f4")).status_code == 200
        assert (await client.get("/private/unique-user-value")).status_code == 404
        assert (await client.get("/failure")).status_code == 500

    output = metric_text(metrics)
    assert (
        'coder_manager_api_requests_total{method="GET",route="/items/{item_id}",status="200"} 1.0'
        in output
    )
    assert (
        'coder_manager_api_requests_total{method="GET",route="unmatched",status="404"} 1.0'
        in output
    )
    assert (
        'coder_manager_api_requests_total{method="GET",route="/failure",status="500"} 1.0' in output
    )
    assert (
        'coder_manager_api_request_duration_seconds_count{method="GET",route="/failure"} 1.0'
        in output
    )
    assert 'coder_manager_api_requests_in_progress{method="GET"} 0.0' in output
    assert "3f5acb75-fd25-4d5f-b522-a9b8085529f4" not in output
    assert "unique-user-value" not in output


@pytest.mark.parametrize("path", ["/health", "/metrics"])
async def test_api_application_port_does_not_expose_observability_endpoints(
    client: AsyncClient,
    path: str,
) -> None:
    """Keep observability endpoints off the API application port."""

    response = await client.get(path)

    assert response.status_code == 404


def test_worker_metrics_record_one_terminal_state_and_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Record worker task activity once and ignore duplicate terminal signals."""

    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    metrics = CeleryMetrics()
    task_name = "coder_manager.healthcheck"
    metrics.initialize_component("worker", (task_name, "celery.backend_cleanup"))
    metrics.task_started("task-id", task_name)
    metrics.task_finished("task-id", task_name, "SUCCESS")
    metrics.task_finished("task-id", task_name, "FAILURE")

    output = metric_text(metrics)
    assert 'coder_manager_celery_up{component="worker"} 1.0' in output
    assert (
        f'coder_manager_celery_worker_tasks_total{{state="success",task="{task_name}"}} 1.0'
        in output
    )
    assert (
        f'coder_manager_celery_worker_tasks_total{{state="failure",task="{task_name}"}} 0.0'
        in output
    )
    assert (
        f'coder_manager_celery_worker_task_duration_seconds_count{{task="{task_name}"}} 1.0'
        in output
    )
    assert f'coder_manager_celery_worker_tasks_in_progress{{task="{task_name}"}} 0.0' in output
    assert "celery.backend_cleanup" not in output


def test_beat_metrics_count_only_beat_publications(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expose Beat publications without creating worker execution series."""

    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    metrics = CeleryMetrics()
    metrics.task_published("coder_manager.before_beat")
    metrics.initialize_component("beat")
    metrics.task_published("celery.backend_cleanup")
    metrics.task_published("coder_manager.retry_job_executions")

    output = metric_text(metrics)
    assert 'coder_manager_celery_up{component="beat"} 1.0' in output
    assert (
        "coder_manager_celery_beat_tasks_published_total"
        '{task="coder_manager.retry_job_executions"} 1.0' in output
    )
    assert "before_beat" not in output
    assert "backend_cleanup" not in output
    assert "coder_manager_celery_worker_tasks_total" not in output


def test_celery_signal_handlers_route_worker_and_beat_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connect the Celery signals to their component-specific metric owner."""

    calls: list[tuple[object, ...]] = []
    fake_metrics = SimpleNamespace(
        initialize_component=lambda *args: calls.append(("initialize", *args)),
        start_server=lambda *args: calls.append(("start", *args)),
        stop_server=lambda: calls.append(("stop",)),
        task_started=lambda *args: calls.append(("task_started", *args)),
        task_finished=lambda *args: calls.append(("task_finished", *args)),
        task_published=lambda *args: calls.append(("task_published", *args)),
    )
    monkeypatch.setattr(celery_app, "celery_metrics", fake_metrics)
    task = SimpleNamespace(name="coder_manager.healthcheck")

    celery_app.initialize_worker_metrics()
    celery_app.start_worker_metrics_server()
    celery_app.record_worker_task_start(task_id="id", task=task)
    celery_app.record_worker_task_finish(task_id="id", task=task, state="SUCCESS")
    celery_app.record_beat_task_publication(sender="coder_manager.healthcheck")
    celery_app.shutdown_worker_metrics_server()

    assert calls[0][0:2] == ("initialize", "worker")
    assert calls[1][0:2] == ("initialize", "worker")
    assert ("start", celery_app.settings.metrics_host, celery_app.settings.metrics_port) in calls
    assert ("task_started", "id", "coder_manager.healthcheck") in calls
    assert ("task_finished", "id", "coder_manager.healthcheck", "SUCCESS") in calls
    assert ("task_published", "coder_manager.healthcheck") in calls
    assert calls[-1] == ("stop",)


def test_worker_init_requires_public_url_config_without_affecting_beat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail worker startup on missing region while keeping Beat initialization independent."""

    calls: list[tuple[object, ...]] = []
    fake_metrics = SimpleNamespace(
        initialize_component=lambda *args: calls.append(("initialize", *args)),
        start_server=lambda *args: calls.append(("start", *args)),
    )
    monkeypatch.setattr(celery_app, "celery_metrics", fake_metrics)
    monkeypatch.setattr(celery_app, "settings", Settings(argocd_region=None))

    validation_step = celery_app.ValidateInstancePublicUrlConfig(None)
    with pytest.raises(ValueError, match="CODER_MANAGER_ARGOCD_REGION is required"):
        validation_step.start(None)

    celery_app.initialize_worker_metrics()
    celery_app.initialize_beat_metrics()

    assert calls[0][0:2] == ("initialize", "worker")
    assert calls[1:] == [
        ("initialize", "beat"),
        ("start", celery_app.settings.metrics_host, celery_app.settings.metrics_port),
    ]


def test_prepare_multiprocess_directory_removes_only_metric_databases(tmp_path: Path) -> None:
    """Clear stale metric files while retaining unrelated temporary content."""

    directory = tmp_path / "metrics"
    directory.mkdir()
    (directory / "counter_1.db").write_bytes(b"stale")
    retained = directory / "README"
    retained.write_text("retain", encoding="utf-8")

    assert prepare_multiprocess_directory(str(directory)) == directory
    assert not (directory / "counter_1.db").exists()
    assert retained.read_text(encoding="utf-8") == "retain"


def test_worker_multiprocess_registry_aggregates_child_metrics(tmp_path: Path) -> None:
    """Aggregate task metrics written by multiple prefork-style child processes."""

    directory = tmp_path / "multiprocess"
    directory.mkdir()
    script = """
import os
from prometheus_client import generate_latest
from coder_manager.metrics import CeleryMetrics

metrics = CeleryMetrics()
metrics.initialize_component("worker", ("coder_manager.healthcheck",))
children = []
for index in range(2):
    pid = os.fork()
    if pid == 0:
        task_id = f"task-{index}"
        metrics.task_started(task_id, "coder_manager.healthcheck")
        metrics.task_finished(task_id, "coder_manager.healthcheck", "SUCCESS")
        os._exit(0)
    children.append(pid)
for pid in children:
    os.waitpid(pid, 0)
print(generate_latest(metrics.exposition_registry()).decode())
"""
    environment = dict(os.environ, PROMETHEUS_MULTIPROC_DIR=str(directory))
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert (
        "coder_manager_celery_worker_tasks_total"
        '{state="success",task="coder_manager.healthcheck"} 2.0' in completed.stdout
    )
    assert (
        "coder_manager_celery_worker_task_duration_seconds_count"
        '{task="coder_manager.healthcheck"} 2.0' in completed.stdout
    )
    assert (
        'coder_manager_celery_worker_tasks_in_progress{task="coder_manager.healthcheck"} 0.0'
        in completed.stdout
    )


@pytest.mark.parametrize("port", [0, 65536])
def test_metrics_port_rejects_values_outside_tcp_range(port: int) -> None:
    """Validate that the configured metrics port is usable by TCP."""

    with pytest.raises(ValueError, match="metrics_port"):
        Settings(metrics_port=port)
