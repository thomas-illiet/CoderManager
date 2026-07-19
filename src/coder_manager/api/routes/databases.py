"""Managed PostgreSQL database pool endpoints."""

from collections import defaultdict
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from coder_manager.config import Settings, get_settings
from coder_manager.crypto import (
    CryptoConfigurationError,
    PasswordCipher,
    PasswordDecryptionError,
)
from coder_manager.database import get_session
from coder_manager.models import Database, InstanceRegion
from coder_manager.repositories import (
    DatabaseAlreadyExistsError,
    DatabaseCapacityConflictError,
    DatabaseInUseError,
    DatabaseNotFoundError,
    DatabaseRegionConflictError,
    DatabaseRepository,
    DatabaseUsage,
    InstanceRepository,
)
from coder_manager.schemas import (
    DatabaseCreate,
    DatabaseItemStatistics,
    DatabaseListQuery,
    DatabasePage,
    DatabaseRead,
    DatabaseRegionStatistics,
    DatabaseStatistics,
    DatabaseUpdate,
    InstancePage,
    InstanceRead,
)
from coder_manager.tasks import sync_database as sync_database_job

router = APIRouter(prefix="/databases", tags=["databases"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]
DATABASE_CONNECTION_TIMEOUT_SECONDS = 5.0


def password_cipher(settings: Settings) -> PasswordCipher:
    """Build the password cipher or return a redacted configuration error."""

    try:
        return PasswordCipher(settings.crypto_key)
    except CryptoConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database password encryption is not configured",
        ) from error


def utilization_percent(*, allocated: int, capacity: int) -> float:
    """Return a stable percentage rounded to two decimal places."""

    if capacity == 0:
        return 0.0
    return round(allocated / capacity * 100, 2)


def database_read(usage: DatabaseUsage) -> DatabaseRead:
    """Build a database representation without password material."""

    database = usage.database
    return DatabaseRead(
        id=database.id,
        name=database.name,
        region=database.region,
        instance_max=database.instance_max,
        host=database.host,
        port=database.port,
        database_name=database.database_name,
        username=database.username,
        password_configured=bool(database.password_enc),
        allocated_instances=usage.allocated_instances,
        available_slots=max(database.instance_max - usage.allocated_instances, 0),
        created_at=database.created_at,
        updated_at=database.updated_at,
    )


async def check_managed_database_connection(
    database: Database,
    password: SecretStr,
) -> None:
    """Open and validate one managed PostgreSQL connection."""

    connection = await asyncpg.connect(
        host=database.host,
        port=database.port,
        database=database.database_name,
        user=database.username,
        password=password.get_secret_value(),
        timeout=DATABASE_CONNECTION_TIMEOUT_SECONDS,
    )
    try:
        await connection.execute("SELECT 1")
    finally:
        await connection.close(timeout=DATABASE_CONNECTION_TIMEOUT_SECONDS)


@router.get("", summary="List managed databases")
async def list_databases(
    session: SessionDependency,
    query: Annotated[DatabaseListQuery, Query()],
) -> DatabasePage:
    """Return a filtered page with current allocation counts."""

    usages, total = await DatabaseRepository(session).list_page(
        page=query.page,
        page_size=query.page_size,
        region=query.region,
        name=query.name,
    )
    pages = (total + query.page_size - 1) // query.page_size
    return DatabasePage(
        items=[database_read(usage) for usage in usages],
        page=query.page,
        page_size=query.page_size,
        total=total,
        pages=pages,
    )


@router.get("/statistics", summary="Get managed database usage statistics")
async def get_database_statistics(session: SessionDependency) -> DatabaseStatistics:
    """Return global, regional, and per-database capacity statistics."""

    # Group the repository snapshot once so all aggregate views stay consistent.
    usages = await DatabaseRepository(session).list_usage()
    regional_usages: dict[InstanceRegion, list[DatabaseUsage]] = defaultdict(list)
    for usage in usages:
        regional_usages[usage.database.region].append(usage)

    # Build regional totals in enum order for a stable API response.
    total_capacity = sum(usage.database.instance_max for usage in usages)
    allocated_instances = sum(usage.allocated_instances for usage in usages)
    regions: list[DatabaseRegionStatistics] = []
    for region in InstanceRegion:
        region_usages = regional_usages.get(region, [])
        if not region_usages:
            continue
        region_capacity = sum(usage.database.instance_max for usage in region_usages)
        region_allocated = sum(usage.allocated_instances for usage in region_usages)
        regions.append(
            DatabaseRegionStatistics(
                region=region,
                database_count=len(region_usages),
                total_capacity=region_capacity,
                allocated_instances=region_allocated,
                available_slots=max(region_capacity - region_allocated, 0),
                utilization_percent=utilization_percent(
                    allocated=region_allocated,
                    capacity=region_capacity,
                ),
            )
        )

    # Reuse the same snapshot for global and per-database utilization values.
    return DatabaseStatistics(
        database_count=len(usages),
        total_capacity=total_capacity,
        allocated_instances=allocated_instances,
        available_slots=max(total_capacity - allocated_instances, 0),
        utilization_percent=utilization_percent(
            allocated=allocated_instances,
            capacity=total_capacity,
        ),
        regions=regions,
        databases=[
            DatabaseItemStatistics(
                id=usage.database.id,
                name=usage.database.name,
                region=usage.database.region,
                instance_max=usage.database.instance_max,
                allocated_instances=usage.allocated_instances,
                available_slots=max(
                    usage.database.instance_max - usage.allocated_instances,
                    0,
                ),
                utilization_percent=utilization_percent(
                    allocated=usage.allocated_instances,
                    capacity=usage.database.instance_max,
                ),
            )
            for usage in usages
        ],
    )


@router.post(
    "/sync",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Synchronize managed databases",
)
async def sync_databases() -> dict[str, str]:
    """Enqueue the managed database synchronization placeholder job."""

    sync_database_job.delay()
    return {"status": "accepted"}


@router.get("/{database_id}/instances", summary="List a managed database's Coder instances")
async def list_database_instances(
    database_id: UUID,
    session: SessionDependency,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> InstancePage:
    """Return a page of Coder instances allocated to one managed database."""

    if await DatabaseRepository(session).get_usage(database_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Database not found")
    instances, total = await InstanceRepository(session).list(
        page=page,
        page_size=page_size,
        database_id=database_id,
    )
    pages = (total + page_size - 1) // page_size
    return InstancePage(
        items=[InstanceRead.model_validate(instance) for instance in instances],
        page=page,
        page_size=page_size,
        total=total,
        pages=pages,
    )


@router.get("/{database_id}/check", summary="Check a managed database connection")
async def check_database(
    database_id: UUID,
    session: SessionDependency,
    settings: SettingsDependency,
) -> dict[str, str]:
    """Verify that the stored credentials can connect to the managed database."""

    usage = await DatabaseRepository(session).get_usage(database_id)
    if usage is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Database not found")
    try:
        password = password_cipher(settings).decrypt(
            usage.database.password_enc,
            usage.database.id,
        )
    except PasswordDecryptionError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database password cannot be decrypted",
        ) from error
    try:
        await check_managed_database_connection(usage.database, password)
    except (asyncpg.PostgresError, OSError, TimeoutError) as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Database connection is unavailable",
        ) from error
    return {"status": "ok"}


@router.get("/{database_id}", summary="Get a managed database")
async def get_database(database_id: UUID, session: SessionDependency) -> DatabaseRead:
    """Return one managed database or a 404 response."""

    usage = await DatabaseRepository(session).get_usage(database_id)
    if usage is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Database not found")
    return database_read(usage)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a managed database")
async def create_database(
    payload: DatabaseCreate,
    session: SessionDependency,
    settings: SettingsDependency,
) -> DatabaseRead:
    """Encrypt the password and add one database to the regional pool."""

    try:
        usage = await DatabaseRepository(session).create(payload, password_cipher(settings))
    except DatabaseAlreadyExistsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A database with this name already exists",
        ) from error
    return database_read(usage)


@router.put("/{database_id}", summary="Replace a managed database's mutable fields")
async def update_database(
    database_id: UUID,
    payload: DatabaseUpdate,
    session: SessionDependency,
    settings: SettingsDependency,
) -> DatabaseRead:
    """Update connection metadata, capacity, and optionally rotate the password."""

    try:
        usage = await DatabaseRepository(session).update(
            database_id,
            payload,
            password_cipher(settings),
        )
    except DatabaseNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Database not found",
        ) from error
    except DatabaseAlreadyExistsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A database with this name already exists",
        ) from error
    except DatabaseCapacityConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="instance_max cannot be lower than current allocations",
        ) from error
    except DatabaseRegionConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A database with allocations cannot change region",
        ) from error
    return database_read(usage)


@router.delete(
    "/{database_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a managed database",
)
async def delete_database(database_id: UUID, session: SessionDependency) -> Response:
    """Delete an unused database or return a conflict response."""

    try:
        await DatabaseRepository(session).delete(database_id)
    except DatabaseNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Database not found",
        ) from error
    except DatabaseInUseError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Database still has allocated instances",
        ) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
