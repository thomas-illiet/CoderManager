FROM ghcr.io/astral-sh/uv:0.8.11 AS uv
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        cargo \
        libffi-dev \
        libssl-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations

RUN if [ "$(dpkg --print-architecture)" = "arm64" ]; then \
        uv sync --frozen --no-dev --no-binary-package cryptography; \
    else \
        uv sync --frozen --no-dev; \
    fi

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home appuser

COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH"

USER appuser

CMD ["uvicorn", "coder_manager.main:app", "--host", "0.0.0.0", "--port", "8000"]
