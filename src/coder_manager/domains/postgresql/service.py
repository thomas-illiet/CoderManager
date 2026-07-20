"""Synchronous managed PostgreSQL schema service for Celery workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import psycopg
from psycopg import sql

if TYPE_CHECKING:
    from pydantic import SecretStr

CONNECTION_TIMEOUT_SECONDS = 5


@dataclass(frozen=True, slots=True)
class SchemaTarget:
    """Connection metadata and schema identifier for one managed allocation."""

    host: str
    port: int
    database_name: str
    username: str
    password: SecretStr
    schema_name: str


def _connect(
    *,
    host: str,
    port: int,
    database_name: str,
    username: str,
    password: SecretStr,
) -> psycopg.Connection[tuple[object, ...]]:
    """Open a bounded synchronous PostgreSQL connection."""

    return psycopg.connect(
        host=host,
        port=port,
        dbname=database_name,
        user=username,
        password=password.get_secret_value(),
        connect_timeout=CONNECTION_TIMEOUT_SECONDS,
    )


def create_schema(
    target: SchemaTarget,
) -> None:
    """Create an isolated schema idempotently."""

    with (
        _connect(
            host=target.host,
            port=target.port,
            database_name=target.database_name,
            username=target.username,
            password=target.password,
        ) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(target.schema_name))
        )


def drop_schema(
    target: SchemaTarget,
) -> None:
    """Drop an isolated schema and all contained objects idempotently."""

    with (
        _connect(
            host=target.host,
            port=target.port,
            database_name=target.database_name,
            username=target.username,
            password=target.password,
        ) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(target.schema_name))
        )
