FROM ghcr.io/astral-sh/uv:0.8.11 AS uv
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations

RUN uv sync --frozen --no-dev

RUN useradd --create-home appuser

ENV PATH="/app/.venv/bin:$PATH"

USER appuser

CMD ["uvicorn", "coder_manager.main:app", "--host", "0.0.0.0", "--port", "8000"]
