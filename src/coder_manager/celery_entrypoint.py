"""Celery command wrapper for Prometheus multiprocess initialization."""

import os


def run() -> None:
    """Prepare the worker metric directory before importing and running Celery."""

    directory = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if directory:
        from coder_manager.metrics import prepare_multiprocess_directory  # noqa: PLC0415

        prepare_multiprocess_directory(directory)

    from celery.__main__ import main  # noqa: PLC0415

    main()
