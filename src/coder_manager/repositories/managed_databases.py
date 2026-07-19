"""Persistence operations for the managed PostgreSQL database pool."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from coder_manager.models import Database, DatabaseAllocation, InstanceRegion

if TYPE_CHECKING:
    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession

    from coder_manager.crypto import PasswordCipher
    from coder_manager.schemas import DatabaseCreate, DatabaseUpdate


class DatabaseAlreadyExistsError(Exception):
    """Raised when a database name conflicts case-insensitively."""


class DatabaseNotFoundError(Exception):
    """Raised when a managed database cannot be found."""


class DatabaseInUseError(Exception):
    """Raised when deleting a database that still owns allocations."""


class DatabaseCapacityConflictError(Exception):
    """Raised when a capacity update would be lower than current usage."""


class DatabaseRegionConflictError(Exception):
    """Raised when changing the region of a database with allocations."""


@dataclass(frozen=True, slots=True)
class DatabaseUsage:
    """One database paired with its allocation count."""

    database: Database
    allocated_instances: int


class DatabaseRepository:
    """Store managed databases and derive capacity from allocation rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the database session used by repository operations."""

        self._session = session

    @staticmethod
    def _usage_statement() -> Select[tuple[Database, int]]:
        """Build the grouped query that pairs databases with allocation counts."""

        return (
            select(Database, func.count(DatabaseAllocation.id))
            .outerjoin(DatabaseAllocation, DatabaseAllocation.database_id == Database.id)
            .group_by(Database.id)
        )

    async def list_page(
        self,
        *,
        page: int,
        page_size: int,
        region: InstanceRegion | None = None,
        name: str | None = None,
    ) -> tuple[list[DatabaseUsage], int]:
        """Return a filtered page with allocation counts."""

        count_statement = select(func.count()).select_from(Database)
        usage_statement = self._usage_statement()
        if region is not None:
            count_statement = count_statement.where(Database.region == region)
            usage_statement = usage_statement.where(Database.region == region)
        if name is not None:
            condition = Database.name.icontains(name, autoescape=True)
            count_statement = count_statement.where(condition)
            usage_statement = usage_statement.where(condition)

        total = await self._session.scalar(count_statement)
        rows = await self._session.execute(
            usage_statement.order_by(func.lower(Database.name), Database.name, Database.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return [DatabaseUsage(database, count) for database, count in rows], total or 0

    async def list_usage(self) -> list[DatabaseUsage]:
        """Return usage for every database in deterministic regional order."""

        rows = await self._session.execute(
            self._usage_statement().order_by(
                Database.region,
                func.lower(Database.name),
                Database.name,
                Database.id,
            )
        )
        return [DatabaseUsage(database, count) for database, count in rows]

    async def get_usage(self, database_id: UUID) -> DatabaseUsage | None:
        """Find one database and its allocation count."""

        row = (
            await self._session.execute(self._usage_statement().where(Database.id == database_id))
        ).one_or_none()
        if row is None:
            return None
        database, count = row
        return DatabaseUsage(database, count)

    async def create(self, payload: DatabaseCreate, cipher: PasswordCipher) -> DatabaseUsage:
        """Encrypt the password and add one database to the pool."""

        database_id = uuid4()
        database = Database(
            id=database_id,
            name=payload.name,
            region=payload.region,
            instance_max=payload.instance_max,
            host=payload.host,
            port=payload.port,
            database_name=payload.database_name,
            username=payload.username,
            password_enc=cipher.encrypt(payload.password, database_id),
        )
        self._session.add(database)
        try:
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise DatabaseAlreadyExistsError from error
        await self._session.refresh(database)
        return DatabaseUsage(database, 0)

    async def update(
        self,
        database_id: UUID,
        payload: DatabaseUpdate,
        cipher: PasswordCipher,
    ) -> DatabaseUsage:
        """Replace public fields while preserving or rotating the encrypted password."""

        # Lock the database so capacity and region checks use a stable allocation count.
        database = await self._session.scalar(
            select(Database).where(Database.id == database_id).with_for_update()
        )
        if database is None:
            await self._session.rollback()
            raise DatabaseNotFoundError
        allocated_instances = (
            await self._session.scalar(
                select(func.count())
                .select_from(DatabaseAllocation)
                .where(DatabaseAllocation.database_id == database_id)
            )
            or 0
        )
        if payload.instance_max < allocated_instances:
            await self._session.rollback()
            raise DatabaseCapacityConflictError
        if payload.region != database.region and allocated_instances:
            await self._session.rollback()
            raise DatabaseRegionConflictError

        # Apply connection metadata only after allocation invariants have passed.
        database.name = payload.name
        database.region = payload.region
        database.instance_max = payload.instance_max
        database.host = payload.host
        database.port = payload.port
        database.database_name = payload.database_name
        database.username = payload.username
        if payload.password is not None:
            database.password_enc = cipher.encrypt(payload.password, database.id)
        database.updated_at = datetime.now(UTC)
        try:
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise DatabaseAlreadyExistsError from error
        await self._session.refresh(database)
        return DatabaseUsage(database, allocated_instances)

    async def delete(self, database_id: UUID) -> None:
        """Delete an unused database from the pool."""

        database = await self._session.scalar(
            select(Database).where(Database.id == database_id).with_for_update()
        )
        if database is None:
            await self._session.rollback()
            raise DatabaseNotFoundError
        allocation_id = await self._session.scalar(
            select(DatabaseAllocation.id)
            .where(DatabaseAllocation.database_id == database_id)
            .limit(1)
        )
        if allocation_id is not None:
            await self._session.rollback()
            raise DatabaseInUseError
        await self._session.delete(database)
        await self._session.commit()
