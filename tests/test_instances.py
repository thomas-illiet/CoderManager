"""Coder instance API and state transition tests."""

import re
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coder_manager.api.routes import instances as instance_routes
from coder_manager.config import Settings, get_settings
from coder_manager.crypto import (
    InstancePasswordCipher,
    InstancePasswordDecryptionError,
)
from coder_manager.domains import argocd
from coder_manager.main import app
from coder_manager.models import (
    Instance,
    InstanceEnvironment,
    InstanceStatus,
    JobExecution,
    JobStatus,
)
from coder_manager.repositories import (
    InstanceActionConflictError,
    InstanceAlreadyExistsError,
    InstanceDatabaseUnavailableError,
    InstanceNotFoundError,
    InstanceRepository,
    InvalidInstanceActionError,
)
from coder_manager.repositories import instances as instance_repositories
from coder_manager.schemas import InstanceCreate
from coder_manager.tasks.common.registry import (
    INSTANCE_CREATE_STEP_04,
    INSTANCE_CREATE_STEP_04_TASK,
)
from tests.conftest import TEST_CRYPTO_KEY

TEST_INSTANCE_SLUG = "k7m4p2x9q3ab"
SECOND_INSTANCE_SLUG = "m8n5q3y0r4bc"


async def create_instance(
    client: AsyncClient,
    application: str,
    *,
    environment: str = "development",
) -> dict[str, str]:
    """Create and return one Coder instance through the API."""

    response = await client.post(
        "/api/v1/instances",
        json={
            "application": application,
            "environment": environment,
        },
    )
    assert response.status_code == 201
    return response.json()["resource"]


async def test_create_instance_get_and_missing(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the create instance get and missing scenario."""

    monkeypatch.setattr(
        instance_repositories,
        "generate_instance_slug",
        lambda: TEST_INSTANCE_SLUG,
    )
    created = await create_instance(client, " mon équipe / portail ")

    assert set(created) == {
        "id",
        "application",
        "slug",
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
    assert created["application"] == "MON ÉQUIPE / PORTAIL"
    assert created["slug"] == TEST_INSTANCE_SLUG
    assert created["action"] == "creating"
    assert created["status"] == "pending"
    assert created["argocd_application_name"] is None
    assert created["instance_url"] == f"https://{TEST_INSTANCE_SLUG}.code-studio.dev.echonet"
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


async def test_instance_admin_endpoint_returns_static_identity_and_stored_password(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Expose credentials only after a successful durable bootstrap step."""

    created = await create_instance(client, "ADMIN ENDPOINT")
    instance_id = UUID(created["id"])
    password = SecretStr("stored-instance-password")
    async with session_maker() as session:
        instance = await session.get(Instance, instance_id)
        assert instance is not None
        instance.password_enc = InstancePasswordCipher(SecretStr(TEST_CRYPTO_KEY)).encrypt(
            password,
            instance_id,
        )
        session.add(
            JobExecution(
                name="instance.create",
                task_name=INSTANCE_CREATE_STEP_04_TASK,
                resource_type="instance",
                resource_id=instance_id,
                step=INSTANCE_CREATE_STEP_04,
                status=JobStatus.SUCCESS,
            )
        )
        await session.commit()

    response = await client.get(f"/api/v1/instances/{instance_id}/admin")
    regular = await client.get(f"/api/v1/instances/{instance_id}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "username": "admin",
        "email": "admin@coder.local",
        "password": password.get_secret_value(),
    }
    assert "password" not in regular.json()
    assert "password_enc" not in regular.json()


async def test_instance_admin_endpoint_requires_completed_bootstrap(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Keep prepared passwords unavailable until the bootstrap job succeeds."""

    created = await create_instance(client, "ADMIN PENDING")
    instance_id = UUID(created["id"])
    async with session_maker() as session:
        instance = await session.get(Instance, instance_id)
        assert instance is not None
        instance.password_enc = InstancePasswordCipher(SecretStr(TEST_CRYPTO_KEY)).encrypt(
            SecretStr("prepared-but-not-created"),
            instance_id,
        )
        await session.commit()

    pending = await client.get(f"/api/v1/instances/{instance_id}/admin")
    missing = await client.get(f"/api/v1/instances/{uuid4()}/admin")

    assert pending.status_code == 404
    assert pending.headers["cache-control"] == "no-store"
    assert pending.json() == {"detail": "Instance admin account not initialized"}
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Instance not found"}


async def test_instance_admin_endpoint_redacts_crypto_failures(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Return sanitized service errors for missing or invalid cryptographic material."""

    created = await create_instance(client, "ADMIN CRYPTO")
    instance_id = UUID(created["id"])
    async with session_maker() as session:
        instance = await session.get(Instance, instance_id)
        assert instance is not None
        instance.password_enc = b"invalid-envelope"
        session.add(
            JobExecution(
                name="instance.create",
                task_name=INSTANCE_CREATE_STEP_04_TASK,
                resource_type="instance",
                resource_id=instance_id,
                step=INSTANCE_CREATE_STEP_04,
                status=JobStatus.SUCCESS,
            )
        )
        await session.commit()

    invalid = await client.get(f"/api/v1/instances/{instance_id}/admin")
    app.dependency_overrides[get_settings] = lambda: Settings(crypto_key=None)
    unavailable = await client.get(f"/api/v1/instances/{instance_id}/admin")

    assert invalid.status_code == 503
    assert invalid.json() == {"detail": "Instance admin password cannot be decrypted"}
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Instance password encryption is not configured"}


def test_instance_password_cipher_is_random_and_instance_bound() -> None:
    """Encrypt nondeterministically and reject another instance or damaged envelope."""

    cipher = InstancePasswordCipher(SecretStr(TEST_CRYPTO_KEY))
    password = SecretStr("instance-secret")
    instance_id = uuid4()
    first = cipher.encrypt(password, instance_id)
    second = cipher.encrypt(password, instance_id)

    assert first != second
    assert cipher.decrypt(first, instance_id).get_secret_value() == password.get_secret_value()
    with pytest.raises(InstancePasswordDecryptionError):
        cipher.decrypt(first, uuid4())
    with pytest.raises(InstancePasswordDecryptionError):
        cipher.decrypt(b"invalid", instance_id)


async def test_removed_application_contract_is_rejected(client: AsyncClient) -> None:
    """Reject the removed resource and legacy instance payload without compatibility aliases."""

    endpoint = await client.get("/api/v1/applications")
    legacy_payload = await client.post(
        "/api/v1/instances",
        json={
            "application_id": str(uuid4()),
            "environment": "development",
        },
    )

    assert endpoint.status_code == 404
    assert legacy_payload.status_code == 422


async def test_environment_url_mapping_and_list_filter(client: AsyncClient) -> None:
    """Verify the environment url mapping and list filter scenario."""

    first_application = "FIRST APP"
    second_application = "OTHER APP"
    expected_labels = {
        "development": "dev",
        "staging": "staging",
        "production": "cib",
    }
    for environment, dns_label in expected_labels.items():
        instance = await create_instance(
            client,
            first_application,
            environment=environment,
        )
        assert re.fullmatch(r"[a-z0-9]{12}", instance["slug"])
        assert instance["instance_url"].endswith(f"code-studio.{dns_label}.echonet")
    await create_instance(client, second_application)

    first_page = await client.get("/api/v1/instances", params={"page": 1, "page_size": 2})
    assert first_page.status_code == 200
    assert first_page.json()["total"] == 4
    assert first_page.json()["pages"] == 2
    assert len(first_page.json()["items"]) == 2

    filtered = await client.get(
        "/api/v1/instances",
        params={"application": " first app "},
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 3
    assert all(item["application"] == first_application for item in filtered.json()["items"])


async def test_instance_domain_is_configurable(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build new instance URLs with the configured domain label."""

    monkeypatch.setattr(
        instance_repositories,
        "generate_instance_slug",
        lambda: TEST_INSTANCE_SLUG,
    )
    app.dependency_overrides[get_settings] = lambda: Settings(instance_domain="coder-studio")
    instance = await create_instance(client, "Mon Équipe / Portail")

    assert instance["instance_url"] == f"https://{TEST_INSTANCE_SLUG}.coder-studio.dev.echonet"


async def test_create_rejects_legacy_application_id_and_extra_name(client: AsyncClient) -> None:
    """Reject the removed application_id field and unrelated instance names."""

    legacy = await client.post(
        "/api/v1/instances",
        json={
            "application_id": str(uuid4()),
            "environment": "development",
        },
    )
    with_name = await client.post(
        "/api/v1/instances",
        json={
            "application": "APP",
            "environment": "development",
            "name": "Instances do not have names",
        },
    )

    assert legacy.status_code == 422
    assert with_name.status_code == 422


async def test_invalid_inputs_are_rejected_and_non_dns_applications_are_allowed(
    client: AsyncClient,
) -> None:
    """Reject invalid API values without imposing DNS rules on applications."""

    removed_region = await client.post(
        "/api/v1/instances",
        json={
            "application": "APP",
            "region": "emea",
            "environment": "development",
        },
    )
    invalid_environment = await client.post(
        "/api/v1/instances",
        json={
            "application": "APP",
            "environment": "testing",
        },
    )
    invalid_page = await client.get("/api/v1/instances", params={"page": 0, "page_size": 101})
    empty_application = await client.post(
        "/api/v1/instances",
        json={
            "application": "   ",
            "environment": "development",
        },
    )
    oversized_application = await client.post(
        "/api/v1/instances",
        json={
            "application": "a" * 256,
            "environment": "development",
        },
    )

    punctuation_application = await client.post(
        "/api/v1/instances",
        json={
            "application": "!!!",
            "environment": "development",
        },
    )
    long_application = await client.post(
        "/api/v1/instances",
        json={
            "application": "a" * 64,
            "environment": "development",
        },
    )

    assert removed_region.status_code == 422
    assert invalid_environment.status_code == 422
    assert invalid_page.status_code == 422
    assert empty_application.status_code == 422
    assert oversized_application.status_code == 422
    assert punctuation_application.status_code == 201
    assert long_application.status_code == 201


async def test_placement_conflicts_and_previous_slug_collisions_are_allowed(
    client: AsyncClient,
) -> None:
    """Keep placement uniqueness without deriving URL identity from applications."""

    first = await create_instance(client, "My App")

    duplicate_placement = await client.post(
        "/api/v1/instances",
        json={
            "application": "my app",
            "environment": "development",
        },
    )
    distinct_application = await client.post(
        "/api/v1/instances",
        json={
            "application": "my-app",
            "environment": "development",
        },
    )

    assert duplicate_placement.status_code == 409
    assert distinct_application.status_code == 201
    second = distinct_application.json()["resource"]
    assert first["slug"] != second["slug"]
    assert first["instance_url"] != second["instance_url"]


async def test_slug_collision_regenerates_and_legacy_null_slugs_coexist(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regenerate an occupied slug while allowing nullable historical rows."""

    monkeypatch.setattr(
        instance_repositories,
        "generate_instance_slug",
        lambda: TEST_INSTANCE_SLUG,
    )
    first = await create_instance(client, "FIRST SLUG")
    candidates = iter((TEST_INSTANCE_SLUG, SECOND_INSTANCE_SLUG))
    monkeypatch.setattr(
        instance_repositories,
        "generate_instance_slug",
        lambda: next(candidates),
    )
    second = await create_instance(client, "SECOND SLUG")

    assert first["slug"] == TEST_INSTANCE_SLUG
    assert second["slug"] == SECOND_INSTANCE_SLUG
    async with session_maker() as session:
        first_record = await session.get(Instance, UUID(first["id"]))
        second_record = await session.get(Instance, UUID(second["id"]))
        assert first_record is not None
        assert second_record is not None
        first_record.slug = None
        second_record.slug = None
        await session.commit()


async def test_same_placement_is_allowed_for_different_applications(client: AsyncClient) -> None:
    """Verify the same placement is allowed for different applications scenario."""

    first = await create_instance(client, "First App")
    second = await create_instance(client, "Second App")

    assert first["environment"] == second["environment"] == "development"


async def test_delete_requires_creation_success_and_marks_deleting(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the delete requires creation success and marks deleting scenario."""

    created = await create_instance(client, "APP")

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

    created = await create_instance(client, "APP")
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


def instance_record() -> SimpleNamespace:
    """Build an ORM-shaped instance for isolated route tests."""

    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid4(),
        application="APP",
        slug=TEST_INSTANCE_SLUG,
        environment=InstanceEnvironment.DEVELOPMENT,
        action="creating",
        status=InstanceStatus.PENDING,
        instance_url="https://app.code-studio.dev.echonet",
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
            instance_domain: str,
        ) -> SimpleNamespace:
            """Simulate the repository create operation."""

            assert instance_domain == "code-studio"
            return record

        async def request_deletion(self, _instance_id: UUID) -> SimpleNamespace:
            """Simulate the repository request deletion operation."""

            return record

    monkeypatch.setattr(instance_routes, "InstanceRepository", SuccessfulRepository)
    payload = InstanceCreate(
        application=record.application,
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
            instance_domain: str,
        ) -> None:
            """Simulate the repository create operation."""

            assert instance_domain == "code-studio"
            raise repository_error

    monkeypatch.setattr(instance_routes, "InstanceRepository", FailingRepository)
    payload = InstanceCreate(
        application="APP",
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

    instance = await create_instance(client, "STATUS APP")
    instance_id = UUID(instance["id"])

    def remote_status(
        observed_id: UUID,
        slug: str | None,
        attached_name: str | None,
        _settings: Settings,
    ) -> argocd.ArgoCdApplicationStatus:
        """Simulate the remote status operation used by this scenario."""

        assert observed_id == instance_id
        assert slug == instance["slug"]
        assert attached_name is None
        return argocd.ArgoCdApplicationStatus(
            application_name=f"coder-{instance['slug']}",
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
        "application_name": f"coder-{instance['slug']}",
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
        _slug: str | None,
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
