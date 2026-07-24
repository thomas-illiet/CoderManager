"""Instance Kubernetes provider upload and encryption tests."""

from base64 import b64encode
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coder_manager.config import Settings, get_settings
from coder_manager.crypto import (
    CryptoConfigurationError,
    KubeconfigCipher,
    KubeconfigDecryptionError,
)
from coder_manager.main import app
from coder_manager.models import Instance, InstanceKubernetes, InstanceStatus
from coder_manager.repositories import (
    InstanceActionConflictError,
    InstanceKubernetesAlreadyConfiguredError,
    InstanceKubernetesNotFoundError,
    InstanceKubernetesRepository,
    InstanceNotFoundError,
    InstanceRepository,
)
from coder_manager.tasks import step_01_update_instance
from tests.test_instances import create_instance

CRYPTO_KEY = "MDAxMTIyMzM0NDU1NjY3Nzg4ODlhYWJiY2NkZGVlZmY="
OTHER_CRYPTO_KEY = b64encode(b"x" * 32).decode()
KUBECONFIG = b"\x00\xffarbitrary-kubeconfig\nwith-binary\x80"


def kubeconfig_file(content: bytes = KUBECONFIG) -> dict[str, tuple[str, bytes, str]]:
    """Build one multipart kubeconfig file without constraining its content type."""

    return {"kubeconfig": ("ignored.bin", content, "application/octet-stream")}


async def mark_instance_idle(
    session_maker: async_sessionmaker[AsyncSession],
    instance_id: UUID,
    *,
    expected_action: str,
) -> None:
    """Move an instance's current action to a successful idle state."""

    async with session_maker() as session:
        await InstanceRepository(session).update_action(
            instance_id,
            expected_action=expected_action,
            action=expected_action,
            status=InstanceStatus.SUCCESS,
        )


def test_kubeconfig_cipher_round_trip_and_binding() -> None:
    """Encrypt arbitrary bytes nondeterministically and bind them to one instance UUID."""

    instance_id = uuid4()
    cipher = KubeconfigCipher(SecretStr(CRYPTO_KEY))

    first = cipher.encrypt(KUBECONFIG, instance_id)
    second = cipher.encrypt(KUBECONFIG, instance_id)
    empty = cipher.encrypt(b"", instance_id)

    assert first != second
    assert KUBECONFIG not in first
    assert cipher.decrypt(first, instance_id) == KUBECONFIG
    assert cipher.decrypt(empty, instance_id) == b""
    with pytest.raises(KubeconfigDecryptionError):
        cipher.decrypt(first, uuid4())
    with pytest.raises(KubeconfigDecryptionError):
        KubeconfigCipher(SecretStr(OTHER_CRYPTO_KEY)).decrypt(first, instance_id)
    with pytest.raises(KubeconfigDecryptionError):
        cipher.decrypt(first[:-1] + bytes((first[-1] ^ 1,)), instance_id)
    with pytest.raises(KubeconfigDecryptionError):
        cipher.decrypt(b"invalid-envelope", instance_id)
    with pytest.raises(CryptoConfigurationError):
        KubeconfigCipher(None)


async def test_provider_get_distinguishes_missing_instance_and_configuration(
    client: AsyncClient,
) -> None:
    """Return stable not-found responses for both missing resource levels."""

    instance = await create_instance(client, "PROVIDER APP")

    unconfigured = await client.get(f"/api/v1/instances/{instance['id']}/provider")
    missing = await client.get(f"/api/v1/instances/{uuid4()}/provider")

    assert unconfigured.status_code == 404
    assert unconfigured.json() == {"detail": "Kubernetes provider not configured"}
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Instance not found"}


async def test_provider_upload_encrypts_arbitrary_bytes_and_requests_update(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Encrypt the upload, expose only its status, and dispatch after persistence."""

    instance = await create_instance(client, "PROVIDER APP")
    instance_id = UUID(instance["id"])

    busy = await client.post(
        f"/api/v1/instances/{instance_id}/provider",
        files=kubeconfig_file(),
    )
    assert busy.status_code == 409
    assert busy.json() == {"detail": "Instance has an action in progress"}

    await mark_instance_idle(session_maker, instance_id, expected_action="creating")
    step_01_update_instance.delay.reset_mock()
    created = await client.post(
        f"/api/v1/instances/{instance_id}/provider",
        files=kubeconfig_file(),
    )

    assert created.status_code == 202
    created_resource = created.json()["resource"]
    assert set(created_resource) == {
        "instance_id",
        "kubeconfig_configured",
        "created_at",
        "updated_at",
    }
    assert created_resource["instance_id"] == str(instance_id)
    assert created_resource["kubeconfig_configured"] is True
    assert KUBECONFIG.hex() not in created.text
    assert b64encode(KUBECONFIG).decode() not in created.text
    step_01_update_instance.delay.assert_called_once()

    async with session_maker() as session:
        stored = await session.get(InstanceKubernetes, instance_id)
        instance_record = await InstanceRepository(session).get(instance_id)
        assert stored is not None
        assert KUBECONFIG not in stored.kubeconfig_enc
        assert (
            KubeconfigCipher(SecretStr(CRYPTO_KEY)).decrypt(
                stored.kubeconfig_enc,
                instance_id,
            )
            == KUBECONFIG
        )
        assert instance_record is not None
        assert instance_record.action == "updating"
        assert instance_record.status is InstanceStatus.PENDING

    fetched = await client.get(f"/api/v1/instances/{instance_id}/provider")
    assert fetched.status_code == 200
    assert fetched.json() == created_resource


async def test_provider_accepts_empty_file_and_rejects_missing_or_legacy_payloads(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Accept an empty upload while requiring the multipart field."""

    instance = await create_instance(client, "EMPTY PROVIDER")
    instance_id = UUID(instance["id"])
    await mark_instance_idle(session_maker, instance_id, expected_action="creating")

    missing_file = await client.post(f"/api/v1/instances/{instance_id}/provider")
    legacy_json = await client.post(
        f"/api/v1/instances/{instance_id}/provider",
        json={
            "host": "https://kubernetes.invalid",
            "namespace": "legacy",
            "token": "legacy-secret",
            "ca": "legacy-ca",
        },
    )
    uploaded = await client.post(
        f"/api/v1/instances/{instance_id}/provider",
        files=kubeconfig_file(b""),
    )

    assert missing_file.status_code == 422
    assert legacy_json.status_code == 422
    assert uploaded.status_code == 202
    assert uploaded.json()["resource"]["kubeconfig_configured"] is True
    async with session_maker() as session:
        stored = await session.get(InstanceKubernetes, instance_id)
        assert stored is not None
        assert (
            KubeconfigCipher(SecretStr(CRYPTO_KEY)).decrypt(
                stored.kubeconfig_enc,
                instance_id,
            )
            == b""
        )


async def test_provider_is_create_only_and_redacts_crypto_failures(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Reject duplicate uploads and removed updates without leaking file content."""

    instance = await create_instance(client, "CREATE ONLY PROVIDER")
    instance_id = UUID(instance["id"])
    await mark_instance_idle(session_maker, instance_id, expected_action="creating")
    created = await client.post(
        f"/api/v1/instances/{instance_id}/provider",
        files=kubeconfig_file(),
    )
    assert created.status_code == 202

    await mark_instance_idle(session_maker, instance_id, expected_action="updating")
    duplicate = await client.post(
        f"/api/v1/instances/{instance_id}/provider",
        files=kubeconfig_file(b"replacement"),
    )
    removed_put = await client.put(
        f"/api/v1/instances/{instance_id}/provider",
        files=kubeconfig_file(b"replacement"),
    )

    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Kubernetes provider is already configured"}
    assert removed_put.status_code == 405

    unavailable_instance = await create_instance(client, "CRYPTO FAILURE PROVIDER")
    unavailable_id = UUID(unavailable_instance["id"])
    await mark_instance_idle(session_maker, unavailable_id, expected_action="creating")
    leak_marker = b"must-not-leak-kubeconfig"
    app.dependency_overrides[get_settings] = lambda: Settings(crypto_key="invalid")
    unavailable = await client.post(
        f"/api/v1/instances/{unavailable_id}/provider",
        files=kubeconfig_file(leak_marker),
    )
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Kubeconfig encryption is not configured"}
    assert leak_marker.decode() not in unavailable.text


async def test_provider_repository_covers_resource_and_create_only_boundaries(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Exercise repository-level missing, busy, create, and duplicate transitions."""

    instance = await create_instance(client, "REPOSITORY PROVIDER")
    instance_id = UUID(instance["id"])
    cipher = KubeconfigCipher(SecretStr(CRYPTO_KEY))

    async with session_maker() as session:
        repository = InstanceKubernetesRepository(session)
        with pytest.raises(InstanceKubernetesNotFoundError):
            await repository.get(instance_id)
        with pytest.raises(InstanceNotFoundError):
            await repository.get(uuid4())
        with pytest.raises(InstanceNotFoundError):
            await repository.create_and_request_update(uuid4(), KUBECONFIG, cipher)
        with pytest.raises(InstanceActionConflictError):
            await repository.create_and_request_update(instance_id, KUBECONFIG, cipher)

    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        stored.status = InstanceStatus.ERROR
        await session.commit()
    async with session_maker() as session:
        with pytest.raises(InstanceActionConflictError):
            await InstanceKubernetesRepository(session).create_and_request_update(
                instance_id,
                KUBECONFIG,
                cipher,
            )

    await mark_instance_idle(session_maker, instance_id, expected_action="creating")
    async with session_maker() as session:
        repository = InstanceKubernetesRepository(session)
        created = await repository.create_and_request_update(instance_id, KUBECONFIG, cipher)
        assert await repository.get(instance_id) is created

    await mark_instance_idle(session_maker, instance_id, expected_action="updating")
    async with session_maker() as session:
        with pytest.raises(InstanceKubernetesAlreadyConfiguredError):
            await InstanceKubernetesRepository(session).create_and_request_update(
                instance_id,
                b"replacement",
                cipher,
            )
