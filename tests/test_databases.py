"""Managed database pool, encryption, allocation, and statistics tests."""

# ruff: noqa: S105, S106, S107, SLF001

from base64 import b64encode
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from coder_manager import tasks
from coder_manager.api.routes import databases as database_routes
from coder_manager.config import Settings, get_settings
from coder_manager.crypto import (
    CryptoConfigurationError,
    PasswordCipher,
    PasswordDecryptionError,
)
from coder_manager.main import app
from coder_manager.models import Database, DatabaseAllocation, Instance, InstanceStatus
from coder_manager.repositories import (
    DatabaseAlreadyExistsError,
    DatabaseCapacityConflictError,
    DatabaseInUseError,
    DatabaseNotFoundError,
    DatabaseRegionConflictError,
    InstanceRepository,
)
from coder_manager.schemas import DatabaseCreate, DatabaseUpdate

CRYPTO_KEY = "MDAxMTIyMzM0NDU1NjY3Nzg4ODlhYWJiY2NkZGVlZmY="
OTHER_CRYPTO_KEY = b64encode(b"z" * 32).decode()


def database_payload(
    name: str,
    *,
    region: str = "emea",
    instance_max: int = 2,
    password: str = "database-secret",
) -> dict[str, object]:
    """Build one valid managed database request."""

    return {
        "name": name,
        "region": region,
        "instance_max": instance_max,
        "host": f"{name.lower()}.postgres.internal",
        "port": 5432,
        "database_name": "coder",
        "username": "coder_admin",
        "password": password,
    }


async def clear_database_pool(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Remove fixture pool entries before an isolated placement scenario."""

    async with session_maker() as session:
        await session.execute(delete(DatabaseAllocation))
        await session.execute(delete(Database))
        await session.commit()


async def create_database(client: AsyncClient, **overrides: object) -> dict[str, object]:
    """Create one managed database through the API."""

    payload = database_payload("Primary")
    payload.update(overrides)
    response = await client.post("/api/v1/databases", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def create_application(client: AsyncClient, suffix: str) -> dict[str, object]:
    """Create and whitelist an application for instance allocation tests."""

    response = await client.post(
        "/api/v1/applications",
        json={"external_id": f"app-{suffix}", "name": f"App {suffix}"},
    )
    assert response.status_code == 201
    application = response.json()
    whitelist = await client.post(f"/api/v1/applications/{application['id']}/whitelist")
    assert whitelist.status_code == 204
    return application


async def create_coder_instance(
    client: AsyncClient,
    application_id: object,
    *,
    region: str = "emea",
) -> dict[str, object]:
    """Create one Coder instance through the API."""

    response = await client.post(
        "/api/v1/instances",
        json={
            "application_id": application_id,
            "region": region,
            "environment": "development",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_password_cipher_round_trip_and_failures() -> None:
    """Verify the password cipher round trip and failures scenario."""

    database_id = uuid4()
    cipher = PasswordCipher(SecretStr(CRYPTO_KEY))
    password = SecretStr("very-secret-password")

    first = cipher.encrypt(password, database_id)
    second = cipher.encrypt(password, database_id)

    assert first != second
    assert b"very-secret-password" not in first
    assert cipher.decrypt(first, database_id).get_secret_value() == "very-secret-password"
    with pytest.raises(PasswordDecryptionError):
        cipher.decrypt(first, uuid4())
    with pytest.raises(PasswordDecryptionError):
        PasswordCipher(SecretStr(OTHER_CRYPTO_KEY)).decrypt(first, database_id)
    with pytest.raises(PasswordDecryptionError):
        cipher.decrypt(first[:-1] + bytes((first[-1] ^ 1,)), database_id)
    with pytest.raises(PasswordDecryptionError):
        cipher.decrypt(b"\x02invalid", database_id)
    with pytest.raises(CryptoConfigurationError):
        PasswordCipher(None)
    with pytest.raises(CryptoConfigurationError):
        PasswordCipher(SecretStr("not-base64"))
    with pytest.raises(CryptoConfigurationError):
        PasswordCipher(SecretStr(b64encode(b"short").decode()))


async def test_database_route_error_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Map every repository conflict to its stable HTTP response."""

    create_payload = DatabaseCreate.model_validate(database_payload("Route Test"))
    update_payload = DatabaseUpdate.model_validate(database_payload("Route Test"))
    settings = Settings(crypto_key=CRYPTO_KEY)

    class FailingRepository:
        """Provide the failing repository test double for this scenario."""

        error: type[Exception] = DatabaseNotFoundError

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def get_usage(self, _database_id: UUID) -> None:
            """Simulate the repository get usage operation."""

            return

        async def create(self, *_args: object) -> None:
            """Simulate the repository create operation."""

            raise self.error

        async def update(self, *_args: object) -> None:
            """Simulate the repository update operation."""

            raise self.error

        async def delete(self, *_args: object) -> None:
            """Simulate the repository delete operation."""

            raise self.error

    monkeypatch.setattr(database_routes, "DatabaseRepository", FailingRepository)

    with pytest.raises(HTTPException) as missing_get:
        await database_routes.get_database(uuid4(), None)
    assert missing_get.value.status_code == 404

    FailingRepository.error = DatabaseAlreadyExistsError
    with pytest.raises(HTTPException) as duplicate_create:
        await database_routes.create_database(create_payload, None, settings)
    assert duplicate_create.value.status_code == 409

    for repository_error, expected_detail in (
        (DatabaseNotFoundError, "Database not found"),
        (DatabaseAlreadyExistsError, "A database with this name already exists"),
        (
            DatabaseCapacityConflictError,
            "instance_max cannot be lower than current allocations",
        ),
        (DatabaseRegionConflictError, "A database with allocations cannot change region"),
    ):
        FailingRepository.error = repository_error
        with pytest.raises(HTTPException) as update_error:
            await database_routes.update_database(uuid4(), update_payload, None, settings)
        assert update_error.value.status_code in {404, 409}
        assert update_error.value.detail == expected_detail

    for repository_error, expected_status in (
        (DatabaseNotFoundError, 404),
        (DatabaseInUseError, 409),
    ):
        FailingRepository.error = repository_error
        with pytest.raises(HTTPException) as delete_error:
            await database_routes.delete_database(uuid4(), None)
        assert delete_error.value.status_code == expected_status


async def test_database_crud_encrypts_and_rotates_password(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the database crud encrypts and rotates password scenario."""

    created = await create_database(client, name="Finance EMEA", password="first-secret")

    assert created["password_configured"] is True
    assert "password" not in created
    assert "first-secret" not in str(created)
    database_id = UUID(str(created["id"]))
    async with session_maker() as session:
        stored = await session.get(Database, database_id)
        assert stored is not None
        original_envelope = stored.password_enc
        assert b"first-secret" not in original_envelope
        assert (
            PasswordCipher(SecretStr(CRYPTO_KEY))
            .decrypt(original_envelope, database_id)
            .get_secret_value()
            == "first-secret"
        )

    fetched = await client.get(f"/api/v1/databases/{database_id}")
    listed = await client.get(
        "/api/v1/databases",
        params={"region": "emea", "name": "finance"},
    )
    assert fetched.status_code == 200
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    update_payload = database_payload("Finance Europe", instance_max=4)
    update_payload.pop("password")
    preserved = await client.put(f"/api/v1/databases/{database_id}", json=update_payload)
    assert preserved.status_code == 200
    async with session_maker() as session:
        stored = await session.get(Database, database_id)
        assert stored is not None
        assert stored.password_enc == original_envelope

    update_payload["password"] = "rotated-secret"
    rotated = await client.put(f"/api/v1/databases/{database_id}", json=update_payload)
    assert rotated.status_code == 200
    assert "rotated-secret" not in rotated.text
    async with session_maker() as session:
        stored = await session.get(Database, database_id)
        assert stored is not None
        assert stored.password_enc != original_envelope
        assert (
            PasswordCipher(SecretStr(CRYPTO_KEY))
            .decrypt(stored.password_enc, database_id)
            .get_secret_value()
            == "rotated-secret"
        )

    deleted = await client.delete(f"/api/v1/databases/{database_id}")
    missing = await client.get(f"/api/v1/databases/{database_id}")
    assert deleted.status_code == 204
    assert missing.status_code == 404


async def test_database_validation_duplicates_and_crypto_configuration(
    client: AsyncClient,
) -> None:
    """Verify the database validation duplicates and crypto configuration scenario."""

    first = await create_database(client, name="Case Sensitive")
    second = await create_database(client, name="Second Name")
    duplicate = await client.post(
        "/api/v1/databases",
        json=database_payload("case sensitive"),
    )
    invalid = await client.post(
        "/api/v1/databases",
        json=database_payload("Invalid", instance_max=0, password=""),
    )
    leak_marker = "secret-leak-marker" * 300
    invalid_password = await client.post(
        "/api/v1/databases",
        json=database_payload("Invalid Password", password=leak_marker),
    )
    assert duplicate.status_code == 409
    assert invalid.status_code == 422
    assert invalid_password.status_code == 422
    assert leak_marker not in invalid_password.text
    assert "[REDACTED]" in invalid_password.text
    assert "database-secret" not in duplicate.text

    duplicate_update = await client.put(
        f"/api/v1/databases/{first['id']}",
        json=database_payload("Second Name"),
    )
    missing_id = uuid4()
    missing_update = await client.put(
        f"/api/v1/databases/{missing_id}",
        json=database_payload("Missing"),
    )
    missing_delete = await client.delete(f"/api/v1/databases/{missing_id}")
    assert duplicate_update.status_code == 409
    assert duplicate_update.json() == {"detail": "A database with this name already exists"}
    assert missing_update.status_code == 404
    assert missing_delete.status_code == 404

    moved_payload = database_payload("Second Name", region="apac")
    moved_payload.pop("password")
    moved = await client.put(f"/api/v1/databases/{second['id']}", json=moved_payload)
    assert moved.status_code == 200
    assert moved.json()["region"] == "apac"

    app.dependency_overrides[get_settings] = lambda: Settings(crypto_key="invalid")
    unavailable = await client.post(
        "/api/v1/databases",
        json=database_payload("No Crypto", password="must-not-leak"),
    )
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Database password encryption is not configured"}
    assert "must-not-leak" not in unavailable.text


async def test_allocation_balances_by_utilization_and_statistics(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the allocation balances by utilization and statistics scenario."""

    await clear_database_pool(session_maker)
    alpha = await create_database(client, name="Alpha", instance_max=2)
    beta = await create_database(client, name="Beta", instance_max=4)
    await create_database(client, name="APAC", region="apac", instance_max=3)

    instances = []
    for suffix in range(4):
        application = await create_application(client, str(suffix))
        instances.append(await create_coder_instance(client, application["id"]))

    assert [instance["database_id"] for instance in instances] == [
        alpha["id"],
        beta["id"],
        beta["id"],
        alpha["id"],
    ]
    assert all(
        instance["schema_name"] == f"coder_{UUID(str(instance['id'])).hex}"
        for instance in instances
    )

    response = await client.get("/api/v1/databases/statistics")
    assert response.status_code == 200
    statistics = response.json()
    assert statistics["database_count"] == 3
    assert statistics["total_capacity"] == 9
    assert statistics["allocated_instances"] == 4
    assert statistics["available_slots"] == 5
    assert statistics["utilization_percent"] == 44.44
    assert statistics["regions"] == [
        {
            "region": "emea",
            "database_count": 2,
            "total_capacity": 6,
            "allocated_instances": 4,
            "available_slots": 2,
            "utilization_percent": 66.67,
        },
        {
            "region": "apac",
            "database_count": 1,
            "total_capacity": 3,
            "allocated_instances": 0,
            "available_slots": 3,
            "utilization_percent": 0.0,
        },
    ]
    per_database = {item["name"]: item for item in statistics["databases"]}
    assert per_database["Alpha"]["utilization_percent"] == 100.0
    assert per_database["Beta"]["utilization_percent"] == 50.0


async def test_database_instances_are_paginated_and_scoped(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the database instances are paginated and scoped scenario."""

    await clear_database_pool(session_maker)
    database = await create_database(client, name="Instances EMEA", instance_max=3)
    other_database = await create_database(
        client,
        name="Instances APAC",
        region="apac",
        instance_max=1,
    )

    expected_instances = []
    for suffix in ("database-list-one", "database-list-two"):
        application = await create_application(client, suffix)
        expected_instances.append(await create_coder_instance(client, application["id"]))
    other_application = await create_application(client, "database-list-other")
    other_instance = await create_coder_instance(
        client,
        other_application["id"],
        region="apac",
    )

    first_page = await client.get(
        f"/api/v1/databases/{database['id']}/instances",
        params={"page": 1, "page_size": 1},
    )
    second_page = await client.get(
        f"/api/v1/databases/{database['id']}/instances",
        params={"page": 2, "page_size": 1},
    )

    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert first_page.json() | {"items": []} == {
        "items": [],
        "page": 1,
        "page_size": 1,
        "total": 2,
        "pages": 2,
    }
    returned_instances = first_page.json()["items"] + second_page.json()["items"]
    assert {item["id"] for item in returned_instances} == {
        item["id"] for item in expected_instances
    }
    assert all(item["database_id"] == database["id"] for item in returned_instances)
    assert all(item["schema_name"].startswith("coder_") for item in returned_instances)
    assert other_instance["id"] not in {item["id"] for item in returned_instances}
    assert other_database["id"] != database["id"]


async def test_database_instances_empty_missing_and_validation(
    client: AsyncClient,
) -> None:
    """Verify the database instances empty missing and validation scenario."""

    database = await create_database(client, name="Empty Instances")

    empty = await client.get(f"/api/v1/databases/{database['id']}/instances")
    missing = await client.get(f"/api/v1/databases/{uuid4()}/instances")
    invalid_page = await client.get(
        f"/api/v1/databases/{database['id']}/instances",
        params={"page": 0},
    )

    assert empty.status_code == 200
    assert empty.json() == {
        "items": [],
        "page": 1,
        "page_size": 20,
        "total": 0,
        "pages": 0,
    }
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Database not found"}
    assert invalid_page.status_code == 422


async def test_empty_statistics_and_no_capacity_are_atomic(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the empty statistics and no capacity are atomic scenario."""

    await clear_database_pool(session_maker)
    empty = await client.get("/api/v1/databases/statistics")
    assert empty.json() == {
        "database_count": 0,
        "total_capacity": 0,
        "allocated_instances": 0,
        "available_slots": 0,
        "utilization_percent": 0.0,
        "regions": [],
        "databases": [],
    }

    database = await create_database(client, name="Only", instance_max=1)
    first_application = await create_application(client, "first")
    second_application = await create_application(client, "second")
    await create_coder_instance(client, first_application["id"])
    rejected = await client.post(
        "/api/v1/instances",
        json={
            "application_id": second_application["id"],
            "region": "emea",
            "environment": "development",
        },
    )
    assert rejected.status_code == 409
    assert rejected.json() == {"detail": "No database capacity available for region"}
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(Instance)) == 1
        assert await session.scalar(select(func.count()).select_from(DatabaseAllocation)) == 1
        allocation = await session.scalar(select(DatabaseAllocation))
        assert allocation is not None
        assert str(allocation.database_id) == database["id"]


async def test_in_use_database_rejects_capacity_region_and_deletion(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the in use database rejects capacity region and deletion scenario."""

    await clear_database_pool(session_maker)
    database = await create_database(client, name="Busy", instance_max=2)
    for suffix in ("one", "two"):
        application = await create_application(client, suffix)
        await create_coder_instance(client, application["id"])

    payload = database_payload("Busy", instance_max=1)
    capacity = await client.put(f"/api/v1/databases/{database['id']}", json=payload)
    payload = database_payload("Busy", region="apac", instance_max=2)
    region = await client.put(f"/api/v1/databases/{database['id']}", json=payload)
    deletion = await client.delete(f"/api/v1/databases/{database['id']}")

    assert capacity.status_code == 409
    assert region.status_code == 409
    assert deletion.status_code == 409


async def test_instance_deletion_releases_database_slot(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
) -> None:
    """Verify the instance deletion releases database slot scenario."""

    await clear_database_pool(session_maker)
    database = await create_database(client, name="Reusable", instance_max=1)
    first_application = await create_application(client, "delete-first")
    instance = await create_coder_instance(client, first_application["id"])
    instance_id = UUID(str(instance["id"]))

    async with session_maker() as session:
        await InstanceRepository(session).update_action(
            instance_id,
            expected_action="creating",
            action="creating",
            status=InstanceStatus.SUCCESS,
        )
    accepted = await client.delete(f"/api/v1/instances/{instance_id}")
    assert accepted.status_code == 202

    result = tasks._delete_instance(instance_id, sync_session_maker)
    assert result == {"status": "deleted"}

    second_application = await create_application(client, "delete-second")
    replacement = await create_coder_instance(client, second_application["id"])
    assert replacement["database_id"] == database["id"]
