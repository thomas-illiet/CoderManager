.PHONY: dev format lint migrate test up down

dev:
	uv sync --all-groups

format:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run ty check src

migrate:
	uv run alembic upgrade head

test:
	uv run pytest

up:
	docker compose up --build

down:
	docker compose down
