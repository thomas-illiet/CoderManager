"""Coder instance API and state transition tests."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coder_manager.api.routes import applications as application_routes
from coder_manager.api.routes import instances as instance_routes
from coder_manager.config import Settings, get_settings
from coder_manager.domains import argocd
from coder_manager.main import app
from coder_manager.models import InstanceEnvironment, InstanceRegion, InstanceStatus
from coder_manager.repositories import (
    ApplicationHasInstancesError,
    InstanceActionConflictError,
    InstanceAlreadyExistsError,
    InstanceApplicationNotFoundError,
    InstanceApplicationNotWhitelistedError,
    InstanceDatabaseUnavailableError,
    InstanceNotFoundError,
    InstanceRepository,
    InvalidApplicationSlugError,
    InvalidInstanceActionError,
)
from coder_manager.schemas import InstanceCreate


async def create_application(
    client: AsyncClient,
    *,
    external_id: str = "business-app-1",
    name: str = "Mon Équipe / Portail",
    whitelist: bool = True,
) -> dict[str, object]:
    """Create and return one business application through the API."""

    response = await client.post(
        "/api/v1/applications",
        json={"external_id": external_id, "name": name},
    )
    assert response.status_code == 201
    application = response.json()
    if whitelist:
        whitelist_response = await client.post(
            f"/api/v1/applications/{application['id']}/whitelist"
        )
        assert whitelist_response.status_code == 204
    return application


async def create_instance(
    client: AsyncClient,
    application_id: str,
    *,
    region: str = "emea",
    environment: str = "development",
) -> dict[str, str]:
    """Create and return one Coder instance through the API."""

    response = await client.post(
        "/api/v1/instances",
        json={
            "application_id": application_id,
            "region": region,
            "environment": environment,
        },
    )
    assert response.status_code == 201
    return response.json()["resource"]


async def test_create_instance_get_and_missing(client: AsyncClient) -> None:
    """Verify the create instance get and missing scenario."""

    application = await create_application(client)
    created = await create_instance(client, application["id"])

    assert set(created) == {
        "id",
        "application_id",
        "region",
        "environment",
        "action",
        "status",
        "instance_url",
        "argocd_application_name",
        "job_id",
        "step",
        "database_id",
        "schema_name",
        "created_at",
        "updated_at",
    }
    assert created["application_id"] == application["id"]
    assert created["action"] == "creating"
    assert created["status"] == "pending"
    assert created["argocd_application_name"] is None
    assert created["instance_url"] == ("https://mon-equipe-portail.emea.code-studio.dev.echonet")
    assert UUID(created["database_id"])
    assert created["schema_name"] == f"coder_{UUID(created['id']).hex}"
    assert datetime.fromisoformat(created["created_at"])
    assert datetime.fromisoformat(created["updated_at"])

    fetched = await client.get(f"/api/v1/instances/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created

    missing = await client.get(f"/api/v1/instances/{uuid4()}")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Instance not found"}


async def test_instance_creation_requires_whitelisted_application(client: AsyncClient) -> None:
    """Verify the instance creation requires whitelisted application scenario."""

    application = await create_application(client, whitelist=False)

    response = await client.post(
        "/api/v1/instances",
        json={
            "application_id": application["id"],
            "region": "emea",
            "environment": "development",
        },
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Application is not whitelisted"}


async def test_global_whitelist_allows_instance_creation(client: AsyncClient) -> None:
    """Verify the global whitelist allows instance creation scenario."""

    application = await create_application(client, whitelist=False)
    app.dependency_overrides[get_settings] = lambda: Settings(global_whitelist=True)

    created = await create_instance(client, application["id"])

    assert created["application_id"] == application["id"]


async def test_environment_url_mapping_and_list_filter(client: AsyncClient) -> None:
    """Verify the environment url mapping and list filter scenario."""

    first_application = await create_application(client)
    second_application = await create_application(
        client,
        external_id="business-app-2",
        name="Other App",
    )
    expected_labels = {
        "development": "dev",
        "staging": "staging",
        "production": "cib",
    }
    for environment, dns_label in expected_labels.items():
        instance = await create_instance(
            client,
            first_application["id"],
            environment=environment,
        )
        assert instance["instance_url"].endswith(f"code-studio.{dns_label}.echonet")
    await create_instance(client, second_application["id"])

    first_page = await client.get("/api/v1/instances", params={"page": 1, "page_size": 2})
    assert first_page.status_code == 200
    assert first_page.json()["total"] == 4
    assert first_page.json()["pages"] == 2
    assert len(first_page.json()["items"]) == 2

    filtered = await client.get(
        "/api/v1/instances",
        params={"application_id": first_application["id"]},
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 3
    assert all(
        item["application_id"] == first_application["id"] for item in filtered.json()["items"]
    )


async def test_create_rejects_unknown_application_and_extra_name(client: AsyncClient) -> None:
    """Verify the create rejects unknown application and extra name scenario."""

    missing = await client.post(
        "/api/v1/instances",
        json={
            "application_id": str(uuid4()),
            "region": "emea",
            "environment": "development",
        },
    )
    application = await create_application(client)
    with_name = await client.post(
        "/api/v1/instances",
        json={
            "application_id": application["id"],
            "region": "emea",
            "environment": "development",
            "name": "Instances do not have names",
        },
    )

    assert missing.status_code == 404
    assert with_name.status_code == 422


async def test_invalid_enums_pagination_and_application_slugs_are_rejected(
    client: AsyncClient,
) -> None:
    """Verify the invalid enums pagination and application slugs are rejected scenario."""

    application = await create_application(client)
    invalid_region = await client.post(
        "/api/v1/instances",
        json={
            "application_id": application["id"],
            "region": "antarctica",
            "environment": "development",
        },
    )
    invalid_environment = await client.post(
        "/api/v1/instances",
        json={
            "application_id": application["id"],
            "region": "emea",
            "environment": "testing",
        },
    )
    invalid_page = await client.get("/api/v1/instances", params={"page": 0, "page_size": 101})

    symbol_application = await create_application(
        client,
        external_id="symbol-app",
        name="!!!",
    )
    invalid_slug = await client.post(
        "/api/v1/instances",
        json={
            "application_id": symbol_application["id"],
            "region": "emea",
            "environment": "development",
        },
    )
    long_application = await create_application(
        client,
        external_id="long-app",
        name="a" * 64,
    )
    long_slug = await client.post(
        "/api/v1/instances",
        json={
            "application_id": long_application["id"],
            "region": "emea",
            "environment": "development",
        },
    )

    assert invalid_region.status_code == 422
    assert invalid_environment.status_code == 422
    assert invalid_page.status_code == 422
    assert invalid_slug.status_code == 422
    assert long_slug.status_code == 422


async def test_placement_and_url_collisions_return_conflict(client: AsyncClient) -> None:
    """Verify the placement and url collisions return conflict scenario."""

    first_application = await create_application(client, name="My App")
    await create_instance(client, first_application["id"])

    duplicate_placement = await client.post(
        "/api/v1/instances",
        json={
            "application_id": first_application["id"],
            "region": "emea",
            "environment": "development",
        },
    )
    second_application = await create_application(
        client,
        external_id="business-app-2",
        name="my-app",
    )
    duplicate_url = await client.post(
        "/api/v1/instances",
        json={
            "application_id": second_application["id"],
            "region": "emea",
            "environment": "development",
        },
    )

    assert duplicate_placement.status_code == 409
    assert duplicate_url.status_code == 409


async def test_same_placement_is_allowed_for_different_applications(client: AsyncClient) -> None:
    """Verify the same placement is allowed for different applications scenario."""

    first_application = await create_application(client, name="First App")
    second_application = await create_application(
        client,
        external_id="business-app-2",
        name="Second App",
    )

    first = await create_instance(client, first_application["id"])
    second = await create_instance(client, second_application["id"])

    assert first["region"] == second["region"] == "emea"
    assert first["environment"] == second["environment"] == "development"


async def test_delete_requires_creation_success_and_marks_deleting(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the delete requires creation success and marks deleting scenario."""

    application = await create_application(client)
    created = await create_instance(client, application["id"])

    premature = await client.delete(f"/api/v1/instances/{created['id']}")
    assert premature.status_code == 409

    async with session_maker() as session:
        await InstanceRepository(session).update_action(
            UUID(created["id"]),
            expected_action="creating",
            action="creating",
            status=InstanceStatus.SUCCESS,
        )

    accepted = await client.delete(f"/api/v1/instances/{created['id']}")
    assert accepted.status_code == 202
    accepted_resource = accepted.json()["resource"]
    assert accepted_resource["action"] == "deleting"
    assert accepted_resource["status"] == "pending"

    fetched = await client.get(f"/api/v1/instances/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == accepted_resource

    repeated = await client.delete(f"/api/v1/instances/{created['id']}")
    assert repeated.status_code == 409


async def test_delete_missing_instance_returns_not_found(client: AsyncClient) -> None:
    """Verify the delete missing instance returns not found scenario."""

    response = await client.delete(f"/api/v1/instances/{uuid4()}")

    assert response.status_code == 404


async def test_internal_actions_are_free_form_and_stale_updates_are_rejected(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the internal actions are free form and stale updates are rejected scenario."""

    application = await create_application(client)
    created = await create_instance(client, application["id"])
    instance_id = UUID(created["id"])

    async with session_maker() as session:
        repository = InstanceRepository(session)
        updated = await repository.update_action(
            instance_id,
            expected_action="creating",
            action="rebuilding-workspace",
            status=InstanceStatus.RUNNING,
        )
        assert updated.action == "rebuilding-workspace"

        with pytest.raises(InstanceActionConflictError):
            await repository.update_action(
                instance_id,
                expected_action="creating",
                action="creating",
                status=InstanceStatus.ERROR,
            )

        with pytest.raises(InvalidInstanceActionError):
            await repository.update_action(
                instance_id,
                expected_action="rebuilding-workspace",
                action="   ",
                status=InstanceStatus.SUCCESS,
            )

        with pytest.raises(InvalidInstanceActionError):
            await repository.update_action(
                instance_id,
                expected_action="rebuilding-workspace",
                action="a" * 256,
                status=InstanceStatus.SUCCESS,
            )

    fetched = await client.get(f"/api/v1/instances/{created['id']}")
    assert fetched.json()["action"] == "rebuilding-workspace"
    assert fetched.json()["status"] == "running"


async def test_application_with_instances_cannot_be_deleted(client: AsyncClient) -> None:
    """Verify the application with instances cannot be deleted scenario."""

    application = await create_application(client)
    await create_instance(client, application["id"])

    response = await client.delete(f"/api/v1/applications/{application['id']}")

    assert response.status_code == 409
    assert response.json() == {"detail": "Application still has instances"}


async def test_application_delete_conflict_route_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the application delete conflict route mapping scenario."""

    class ProtectedApplicationRepository:
        """Provide the protected application repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def get(self, _application_id: UUID) -> SimpleNamespace:
            """Simulate the repository get operation."""

            return SimpleNamespace(id=_application_id)

        async def delete(self, _application: SimpleNamespace) -> None:
            """Simulate the repository delete operation."""

            raise ApplicationHasInstancesError

    monkeypatch.setattr(
        application_routes,
        "ApplicationRepository",
        ProtectedApplicationRepository,
    )

    with pytest.raises(HTTPException) as caught:
        await application_routes.delete_application(uuid4(), None)

    assert caught.value.status_code == 409


def instance_record() -> SimpleNamespace:
    """Build an ORM-shaped instance for isolated route tests."""

    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid4(),
        application_id=uuid4(),
        region=InstanceRegion.EMEA,
        environment=InstanceEnvironment.DEVELOPMENT,
        action="creating",
        status=InstanceStatus.PENDING,
        instance_url="https://app.emea.code-studio.dev.echonet",
        argocd_application_name="managed-attached",
        created_at=now,
        updated_at=now,
    )


async def test_instance_route_success_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the instance route success mapping scenario."""

    record = instance_record()

    class SuccessfulRepository:
        """Provide the successful repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def list(self, **_kwargs: object) -> tuple[list[SimpleNamespace], int]:
            """Simulate the repository list operation."""

            return [record], 1

        async def get(self, _instance_id: UUID) -> SimpleNamespace:
            """Simulate the repository get operation."""

            return record

        async def create(
            self,
            _payload: InstanceCreate,
            *,
            global_whitelist: bool = False,
        ) -> SimpleNamespace:
            """Simulate the repository create operation."""

            assert global_whitelist is False
            return record

        async def request_deletion(self, _instance_id: UUID) -> SimpleNamespace:
            """Simulate the repository request deletion operation."""

            return record

    monkeypatch.setattr(instance_routes, "InstanceRepository", SuccessfulRepository)
    payload = InstanceCreate(
        application_id=record.application_id,
        region=InstanceRegion.EMEA,
        environment=InstanceEnvironment.DEVELOPMENT,
    )

    page = await instance_routes.list_instances(None, 1, 20, None)
    fetched = await instance_routes.get_instance(record.id, None)
    created = await instance_routes.create_instance(payload, None, Settings())
    deleted = await instance_routes.delete_instance(record.id, None)

    assert page.total == 1
    assert fetched.id == record.id
    assert created.resource.id == record.id
    assert deleted.resource.id == record.id


@pytest.mark.parametrize(
    ("repository_error", "expected_status"),
    [
        (InstanceApplicationNotFoundError, 404),
        (InstanceApplicationNotWhitelistedError, 403),
        (InvalidApplicationSlugError, 422),
        (InstanceAlreadyExistsError, 409),
        (InstanceDatabaseUnavailableError, 409),
    ],
)
async def test_create_instance_route_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
    repository_error: type[Exception],
    expected_status: int,
) -> None:
    """Verify the create instance route error mapping scenario."""

    class FailingRepository:
        """Provide the failing repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def create(
            self,
            _payload: InstanceCreate,
            *,
            global_whitelist: bool = False,
        ) -> None:
            """Simulate the repository create operation."""

            assert global_whitelist is False
            raise repository_error

    monkeypatch.setattr(instance_routes, "InstanceRepository", FailingRepository)
    payload = InstanceCreate(
        application_id=uuid4(),
        region=InstanceRegion.EMEA,
        environment=InstanceEnvironment.DEVELOPMENT,
    )

    with pytest.raises(HTTPException) as caught:
        await instance_routes.create_instance(payload, None, Settings())

    assert caught.value.status_code == expected_status


async def test_get_instance_route_not_found_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the get instance route not found mapping scenario."""

    class MissingRepository:
        """Provide the missing repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def get(self, _instance_id: UUID) -> None:
            """Simulate the repository get operation."""

            return

    monkeypatch.setattr(instance_routes, "InstanceRepository", MissingRepository)

    with pytest.raises(HTTPException) as caught:
        await instance_routes.get_instance(uuid4(), None)

    assert caught.value.status_code == 404


async def test_instance_status_endpoint_returns_remote_argocd_state(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the instance status endpoint returns remote argocd state scenario."""

    application = await create_application(client, external_id="status-app", name="Status App")
    instance = await create_instance(client, application["id"])
    instance_id = UUID(instance["id"])

    def remote_status(
        observed_id: UUID,
        attached_name: str | None,
        _settings: Settings,
    ) -> argocd.ArgoCdApplicationStatus:
        """Simulate the remote status operation used by this scenario."""

        assert observed_id == instance_id
        assert attached_name is None
        return argocd.ArgoCdApplicationStatus(
            application_name=f"coder-{instance_id.hex}",
            sync_status="OutOfSync",
            health_status="Progressing",
            operation_phase="Running",
            revision="abc123",
            reconciled_at="2026-07-19T10:20:30Z",
        )

    monkeypatch.setattr(argocd, "read_instance_application_status", remote_status)
    response = await client.get(f"/api/v1/instances/{instance_id}/status")
    missing = await client.get(f"/api/v1/instances/{uuid4()}/status")

    assert response.status_code == 200
    assert response.json() == {
        "instance_id": str(instance_id),
        "application_name": f"coder-{instance_id.hex}",
        "sync_status": "OutOfSync",
        "health_status": "Progressing",
        "operation_phase": "Running",
        "revision": "abc123",
        "reconciled_at": "2026-07-19T10:20:30Z",
    }
    assert missing.status_code == 404


@pytest.mark.parametrize(
    ("remote_error", "expected_status"),
    [
        (argocd.ArgoCdApplicationNotFoundError("missing"), 404),
        (argocd.ArgoCdConfigurationError("missing config"), 503),
        (argocd.ArgoCdRequestError("remote error"), 502),
        (httpx.ConnectError("connection failed"), 502),
    ],
)
async def test_instance_status_route_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
    remote_error: Exception,
    expected_status: int,
) -> None:
    """Verify the instance status route error mapping scenario."""

    record = instance_record()

    class StatusRepository:
        """Provide the status repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def get(self, _instance_id: UUID) -> SimpleNamespace:
            """Simulate the repository get operation."""

            return record

    def fail_status(
        _instance_id: UUID,
        _attached_name: str | None,
        _settings: Settings,
    ) -> None:
        """Simulate the expected fail status behavior."""

        raise remote_error

    monkeypatch.setattr(instance_routes, "InstanceRepository", StatusRepository)
    monkeypatch.setattr(argocd, "read_instance_application_status", fail_status)

    with pytest.raises(HTTPException) as caught:
        await instance_routes.get_instance_status(record.id, None, Settings())

    assert caught.value.status_code == expected_status


@pytest.mark.parametrize(
    ("repository_error", "expected_status"),
    [
        (InstanceNotFoundError, 404),
        (InstanceActionConflictError, 409),
    ],
)
async def test_delete_instance_route_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
    repository_error: type[Exception],
    expected_status: int,
) -> None:
    """Verify the delete instance route error mapping scenario."""

    class FailingRepository:
        """Provide the failing repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def request_deletion(self, _instance_id: UUID) -> None:
            """Simulate the repository request deletion operation."""

            raise repository_error

    monkeypatch.setattr(instance_routes, "InstanceRepository", FailingRepository)

    with pytest.raises(HTTPException) as caught:
        await instance_routes.delete_instance(uuid4(), None)

    assert caught.value.status_code == expected_status
