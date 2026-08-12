"""Prometheus metrics for the API and Celery processes."""

from __future__ import annotations

import atexit
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
    multiprocess,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from prometheus_client.registry import Collector
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

CeleryComponent = Literal["worker", "beat"]

_CELERY_TASK_STATES = ("failure", "retry", "success")
_HEALTH_RESPONSE = b'{"status":"ok"}'


class _MetricsHTTPServer(ThreadingHTTPServer):
    """Serve independent metric scrapes concurrently."""

    daemon_threads = True
    allow_reuse_address = True


class MetricsServer:
    """Expose one Prometheus registry on an isolated HTTP server."""

    def __init__(self, host: str, port: int, registry: Collector) -> None:
        """Bind the metrics listener without starting its serving thread."""

        handler = _metrics_handler(registry)
        self._server = _MetricsHTTPServer((host, port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="coder-manager-prometheus",
            daemon=True,
        )
        self._stopped = False

    @property
    def port(self) -> int:
        """Return the bound TCP port, including an ephemeral test port."""

        return int(self._server.server_address[1])

    def start(self) -> MetricsServer:
        """Start serving metrics in a daemon thread."""

        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop the listener and wait briefly for its serving thread."""

        if self._stopped:
            return
        self._stopped = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _metrics_handler(registry: Collector) -> type[BaseHTTPRequestHandler]:
    """Build a handler exposing the metrics and health paths."""

    class MetricsHandler(BaseHTTPRequestHandler):
        """Render one registry without logging scrape requests."""

        def do_GET(self) -> None:
            """Serve Prometheus metrics or the process liveness response."""

            path = urlsplit(self.path).path
            if path == "/health":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(_HEALTH_RESPONSE)))
                self.end_headers()
                self.wfile.write(_HEALTH_RESPONSE)
                return
            if path != "/metrics":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            output = generate_latest(registry)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Keep Prometheus scrapes out of application logs."""

    return MetricsHandler


def start_metrics_server(host: str, port: int, registry: Collector) -> MetricsServer:
    """Start a metrics listener and arrange process-exit cleanup."""

    server = MetricsServer(host, port, registry).start()
    atexit.register(server.stop)
    return server


class ApiMetrics:
    """Own the API registry and HTTP request instruments."""

    def __init__(self) -> None:
        """Create an isolated registry with API and process collectors."""

        self.registry = CollectorRegistry()
        ProcessCollector(registry=self.registry)
        PlatformCollector(registry=self.registry)
        GCCollector(registry=self.registry)
        self.up = Gauge(
            "coder_manager_api_up",
            "Whether the Coder Manager API process is running.",
            registry=self.registry,
        )
        self.requests = Counter(
            "coder_manager_api_requests_total",
            "Total API HTTP requests.",
            ("method", "route", "status"),
            registry=self.registry,
        )
        self.duration = Histogram(
            "coder_manager_api_request_duration_seconds",
            "API HTTP request duration in seconds.",
            ("method", "route"),
            registry=self.registry,
        )
        self.in_progress = Gauge(
            "coder_manager_api_requests_in_progress",
            "API HTTP requests currently being processed.",
            ("method",),
            registry=self.registry,
        )
        self.up.set(1)


class ApiMetricsMiddleware:
    """Record FastAPI requests using normalized route templates."""

    def __init__(self, app: ASGIApp, metrics: ApiMetrics) -> None:
        """Wrap an ASGI application with the supplied API instruments."""

        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Observe one HTTP request while passing other ASGI scopes through."""

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope["method"])
        status_code = HTTPStatus.INTERNAL_SERVER_ERROR
        started_at = time.perf_counter()
        self.metrics.in_progress.labels(method).inc()

        async def observe_status(message: Message) -> None:
            """Capture the response status before forwarding the ASGI message."""

            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, observe_status)
        finally:
            route = scope.get("route")
            route_template = str(getattr(route, "path", "unmatched"))
            self.metrics.in_progress.labels(method).dec()
            self.metrics.requests.labels(method, route_template, str(status_code)).inc()
            self.metrics.duration.labels(method, route_template).observe(
                time.perf_counter() - started_at
            )


class CeleryMetrics:
    """Own Celery task instruments and component-specific registries."""

    def __init__(self) -> None:
        """Create metrics compatible with standard and multiprocess runtimes."""

        self.multiprocess_directory = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
        self._metric_registry = None if self.multiprocess_directory else CollectorRegistry()
        self._server: MetricsServer | None = None
        self._component: CeleryComponent | None = None
        self._started_tasks: dict[str, float] = {}
        self.up: Gauge | None = None
        self.worker_tasks: Counter | None = None
        self.worker_duration: Histogram | None = None
        self.worker_in_progress: Gauge | None = None
        self.beat_published: Counter | None = None

    @property
    def component(self) -> CeleryComponent | None:
        """Return the Celery component active in this process."""

        return self._component

    def initialize_component(
        self,
        component: CeleryComponent,
        task_names: Iterable[str] = (),
    ) -> None:
        """Select a runtime component and initialize its expected series."""

        if self._component is not None and self._component != component:
            msg = f"Celery metrics already initialized for {self._component}"
            raise RuntimeError(msg)
        if self._component is None:
            self._create_component_metrics(component)
        self._component = component
        if self.up is None:  # pragma: no cover - component initialization invariant
            msg = "Celery availability metric is unavailable"
            raise RuntimeError(msg)
        self.up.labels(component).set(1)
        if component == "worker":
            if (
                self.worker_tasks is None
                or self.worker_duration is None
                or self.worker_in_progress is None
            ):  # pragma: no cover - component initialization invariant
                msg = "Celery worker metrics are unavailable"
                raise RuntimeError(msg)
            coder_manager_tasks = sorted(
                name for name in task_names if name.startswith("coder_manager.")
            )
            for task_name in coder_manager_tasks:
                for state in _CELERY_TASK_STATES:
                    self.worker_tasks.labels(task_name, state)
                self.worker_duration.labels(task_name)
                self.worker_in_progress.labels(task_name).set(0)

    def _create_component_metrics(self, component: CeleryComponent) -> None:
        """Create only the collectors belonging to the selected component."""

        self.up = Gauge(
            "coder_manager_celery_up",
            "Whether a Coder Manager Celery component is running.",
            ("component",),
            registry=self._metric_registry,
            multiprocess_mode="livesum",
        )
        if component == "worker":
            self.worker_tasks = Counter(
                "coder_manager_celery_worker_tasks_total",
                "Total Celery tasks completed by workers.",
                ("task", "state"),
                registry=self._metric_registry,
            )
            self.worker_duration = Histogram(
                "coder_manager_celery_worker_task_duration_seconds",
                "Celery worker task duration in seconds.",
                ("task",),
                registry=self._metric_registry,
            )
            self.worker_in_progress = Gauge(
                "coder_manager_celery_worker_tasks_in_progress",
                "Celery tasks currently running in worker processes.",
                ("task",),
                registry=self._metric_registry,
                multiprocess_mode="livesum",
            )
        else:
            self.beat_published = Counter(
                "coder_manager_celery_beat_tasks_published_total",
                "Total Celery tasks successfully published by Beat.",
                ("task",),
                registry=self._metric_registry,
            )

    def start_server(self, host: str, port: int) -> None:
        """Start one component metrics listener if it is not already running."""

        if self._server is not None:
            return
        self._server = start_metrics_server(host, port, self.exposition_registry())

    def stop_server(self) -> None:
        """Mark the component down and stop its metrics listener."""

        if self._component is not None and self.up is not None:
            self.up.labels(self._component).set(0)
        if self._server is not None:
            self._server.stop()
            self._server = None

    def exposition_registry(self) -> Collector:
        """Build the registry required by the current process model."""

        if self.multiprocess_directory:
            registry = CollectorRegistry(support_collectors_without_names=True)
            multiprocess.MultiProcessCollector(registry, path=self.multiprocess_directory)
            return registry
        if self._metric_registry is None:  # pragma: no cover - construction invariant
            msg = "standard Celery metrics registry is unavailable"
            raise RuntimeError(msg)
        return self._metric_registry

    def task_started(self, task_id: str, task_name: str) -> None:
        """Record the start of one worker task."""

        if self._component != "worker":
            return
        if self.worker_in_progress is None:  # pragma: no cover - initialization invariant
            msg = "Celery worker in-progress metric is unavailable"
            raise RuntimeError(msg)
        self._started_tasks[task_id] = time.perf_counter()
        self.worker_in_progress.labels(task_name).inc()

    def task_finished(self, task_id: str, task_name: str, state: str | None) -> None:
        """Record exactly one terminal observation for a worker task."""

        if self._component != "worker":
            return
        started_at = self._started_tasks.pop(task_id, None)
        if started_at is None:
            return
        if (
            self.worker_tasks is None
            or self.worker_duration is None
            or self.worker_in_progress is None
        ):  # pragma: no cover - initialization invariant
            msg = "Celery worker metrics are unavailable"
            raise RuntimeError(msg)
        normalized_state = (state or "unknown").lower()
        self.worker_in_progress.labels(task_name).dec()
        self.worker_tasks.labels(task_name, normalized_state).inc()
        self.worker_duration.labels(task_name).observe(time.perf_counter() - started_at)

    def task_published(self, task_name: str) -> None:
        """Record a task publication only when it originates from Beat."""

        if (
            self._component == "beat"
            and self.beat_published is not None
            and task_name.startswith("coder_manager.")
        ):
            self.beat_published.labels(task_name).inc()


def mark_worker_process_dead(pid: int) -> None:
    """Remove live-gauge files for a worker child process that exited."""

    directory = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if directory:
        multiprocess.mark_process_dead(pid, path=directory)


def prepare_multiprocess_directory(directory: str) -> Path:
    """Create the Prometheus directory and remove stale metric database files."""

    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    for metric_file in path.glob("*.db"):
        metric_file.unlink()
    return path
