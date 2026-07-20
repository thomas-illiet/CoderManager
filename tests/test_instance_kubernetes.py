"""Instance Kubernetes provider API and encryption tests."""

from base64 import b64encode
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coder_manager.config import Settings, get_settings
from coder_manager.crypto import (
    CryptoConfigurationError,
    KubernetesTokenCipher,
    KubernetesTokenDecryptionError,
)
from coder_manager.main import app
from coder_manager.models import Instance, InstanceKubernetes, InstanceStatus
from coder_manager.repositories import (
    InstanceActionConflictError,
    InstanceKubernetesAlreadyConfiguredError,
    InstanceKubernetesImmutableFieldError,
    InstanceKubernetesNotFoundError,
    InstanceKubernetesRepository,
    InstanceNotFoundError,
    InstanceRepository,
)
from coder_manager.schemas import InstanceKubernetesCreate, InstanceKubernetesUpdate
from coder_manager.tasks import upsert_instance
from tests.test_instances import create_application, create_instance

CRYPTO_KEY = "MDAxMTIyMzM0NDU1NjY3Nzg4ODlhYWJiY2NkZGVlZmY="
OTHER_CRYPTO_KEY = b64encode(b"x" * 32).decode()


def provider_payload(
    *,
    token: str | None = "kubernetes-secret",  # noqa: S107
) -> dict[str, str]:
    """Build one valid Kubernetes provider request payload."""

    payload = {
        "host": "https://kubernetes.example.test:6443",
        "namespace": "coder-workspaces",
        "ca": "-----BEGIN CERTIFICATE-----\ntest-ca\n-----END CERTIFICATE-----",
    }
    if token is not None:
        payload["token"] = token
    return payload


def provider_update_payload(
    *,
    token: str | None = None,
    host: str | None = None,
    namespace: str | None = None,
) -> dict[str, str]:
    """Build one provider update with optional token and immutable assertions."""

    payload = {"ca": "-----BEGIN CERTIFICATE-----\nupdated-ca\n-----END CERTIFICATE-----"}
    if token is not None:
        payload["token"] = token
    if host is not None:
        payload["host"] = host
    if namespace is not None:
        payload["namespace"] = namespace
    return payload


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


def test_kubernetes_token_cipher_round_trip_and_binding() -> None:
    """Encrypt tokens nondeterministically and bind them to one instance UUID."""

    instance_id = uuid4()
    token = SecretStr("kubernetes-secret")
    cipher = KubernetesTokenCipher(SecretStr(CRYPTO_KEY))

    first = cipher.encrypt(token, instance_id)
    second = cipher.encrypt(token, instance_id)

    assert first != second
    assert b"kubernetes-secret" not in first
    assert cipher.decrypt(first, instance_id).get_secret_value() == "kubernetes-secret"
    with pytest.raises(KubernetesTokenDecryptionError):
        cipher.decrypt(first, uuid4())
    with pytest.raises(KubernetesTokenDecryptionError):
        KubernetesTokenCipher(SecretStr(OTHER_CRYPTO_KEY)).decrypt(first, instance_id)
    with pytest.raises(KubernetesTokenDecryptionError):
        cipher.decrypt(first[:-1] + bytes((first[-1] ^ 1,)), instance_id)
    with pytest.raises(CryptoConfigurationError):
        KubernetesTokenCipher(None)


async def test_provider_get_distinguishes_missing_instance_and_configuration(
    client: AsyncClient,
) -> None:
    """Return stable not-found responses for both missing resource levels."""

    application = await create_application(client)
    instance = await create_instance(client, application["id"])

    unconfigured = await client.get(f"/api/v1/instances/{instance['id']}/provider")
    missing = await client.get(f"/api/v1/instances/{uuid4()}/provider")

    assert unconfigured.status_code == 404
    assert unconfigured.json() == {"detail": "Kubernetes provider not configured"}
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Instance not found"}


async def test_provider_create_and_update_encrypt_token_and_request_instance_update(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Protect token storage, preserve omitted tokens, and enqueue POST and PUT updates."""

    application = await create_application(client)
    instance = await create_instance(client, application["id"])
    instance_id = UUID(instance["id"])

    busy = await client.post(
        f"/api/v1/instances/{instance_id}/provider",
        json=provider_payload(),
    )
    assert busy.status_code == 409
    assert busy.json() == {"detail": "Instance has an action in progress"}

    await mark_instance_idle(session_maker, instance_id, expected_action="creating")
    upsert_instance.delay.reset_mock()
    created = await client.post(
        f"/api/v1/instances/{instance_id}/provider",
        json=provider_payload(),
    )

    assert created.status_code == 202
    assert created.json()["instance_id"] == str(instance_id)
    assert created.json()["host"] == "https://kubernetes.example.test:6443"
    assert created.json()["namespace"] == "coder-workspaces"
    assert created.json()["token_configured"] is True
    assert "token" not in created.json()
    assert "token_enc" not in created.json()
    upsert_instance.delay.assert_called_once_with(str(instance_id))

    async with session_maker() as session:
        stored = await session.get(InstanceKubernetes, instance_id)
        instance_record = await InstanceRepository(session).get(instance_id)
        assert stored is not None
        assert b"kubernetes-secret" not in stored.token_enc
        original_envelope = stored.token_enc
        assert (
            KubernetesTokenCipher(SecretStr(CRYPTO_KEY))
            .decrypt(stored.token_enc, instance_id)
            .get_secret_value()
            == "kubernetes-secret"
        )
        assert instance_record is not None
        assert instance_record.action == "updating"
        assert instance_record.status is InstanceStatus.PENDING

    fetched = await client.get(f"/api/v1/instances/{instance_id}/provider")
    assert fetched.status_code == 200
    assert fetched.json() == created.json()

    await mark_instance_idle(session_maker, instance_id, expected_action="updating")
    updated = await client.put(
        f"/api/v1/instances/{instance_id}/provider",
        json=provider_update_payload(
            host="https://kubernetes.example.test:6443",
            namespace="coder-workspaces",
        ),
    )
    assert updated.status_code == 202
    assert updated.json()["host"] == "https://kubernetes.example.test:6443"
    assert updated.json()["namespace"] == "coder-workspaces"
    assert "updated-ca" in updated.json()["ca"]
    assert upsert_instance.delay.call_count == 2
    async with session_maker() as session:
        stored = await session.get(InstanceKubernetes, instance_id)
        assert stored is not None
        assert stored.token_enc == original_envelope


async def test_provider_requires_initial_token_and_valid_crypto_without_leaks(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Reject unusable initial configurations while redacting token values."""

    application = await create_application(client)
    instance = await create_instance(client, application["id"])
    instance_id = UUID(instance["id"])
    await mark_instance_idle(session_maker, instance_id, expected_action="creating")

    missing_token = await client.post(
        f"/api/v1/instances/{instance_id}/provider",
        json=provider_payload(token=None),
    )
    assert missing_token.status_code == 422

    leak_marker = "secret-token-marker" * 4000
    invalid_token = await client.post(
        f"/api/v1/instances/{instance_id}/provider",
        json=provider_payload(token=leak_marker),
    )
    assert invalid_token.status_code == 422
    assert leak_marker not in invalid_token.text
    assert "[REDACTED]" in invalid_token.text

    app.dependency_overrides[get_settings] = lambda: Settings(crypto_key="invalid")
    unavailable = await client.post(
        f"/api/v1/instances/{instance_id}/provider",
        json=provider_payload(token="must-not-leak"),  # noqa: S106
    )
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Kubernetes token encryption is not configured"}
    assert "must-not-leak" not in unavailable.text


async def test_provider_put_rejects_missing_busy_and_immutable_changes(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Keep host and namespace immutable and preserve create/update resource boundaries."""

    application = await create_application(client, external_id="immutable-provider")
    instance = await create_instance(client, application["id"])
    instance_id = UUID(instance["id"])
    await mark_instance_idle(session_maker, instance_id, expected_action="creating")

    missing_provider = await client.put(
        f"/api/v1/instances/{instance_id}/provider",
        json=provider_update_payload(),
    )
    assert missing_provider.status_code == 404
    assert missing_provider.json() == {"detail": "Kubernetes provider not configured"}

    upsert_instance.delay.reset_mock()
    created = await client.post(
        f"/api/v1/instances/{instance_id}/provider",
        json=provider_payload(),
    )
    assert created.status_code == 202

    busy = await client.put(
        f"/api/v1/instances/{instance_id}/provider",
        json=provider_update_payload(),
    )
    assert busy.status_code == 409
    assert busy.json() == {"detail": "Instance has an action in progress"}

    await mark_instance_idle(session_maker, instance_id, expected_action="updating")
    duplicate = await client.post(
        f"/api/v1/instances/{instance_id}/provider",
        json=provider_payload(),
    )
    changed_host = await client.put(
        f"/api/v1/instances/{instance_id}/provider",
        json=provider_update_payload(host="https://other.example.test:6443"),
    )
    changed_namespace = await client.put(
        f"/api/v1/instances/{instance_id}/provider",
        json=provider_update_payload(namespace="other-namespace"),
    )

    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Kubernetes provider is already configured"}
    for response in (changed_host, changed_namespace):
        assert response.status_code == 409
        assert response.json() == {"detail": "Kubernetes provider host and namespace are immutable"}
    upsert_instance.delay.assert_called_once_with(str(instance_id))

    fetched = await client.get(f"/api/v1/instances/{instance_id}/provider")
    assert fetched.json()["host"] == "https://kubernetes.example.test:6443"
    assert fetched.json()["namespace"] == "coder-workspaces"


async def test_provider_repository_covers_resource_and_rotation_boundaries(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Exercise repository-level create, immutable, and rotation transitions."""

    application = await create_application(client, external_id="repository-provider")
    instance = await create_instance(client, application["id"])
    instance_id = UUID(instance["id"])
    cipher = KubernetesTokenCipher(SecretStr(CRYPTO_KEY))
    initial_payload = InstanceKubernetesCreate.model_validate(provider_payload())

    async with session_maker() as session:
        repository = InstanceKubernetesRepository(session)
        with pytest.raises(InstanceKubernetesNotFoundError):
            await repository.get(instance_id)
        with pytest.raises(InstanceNotFoundError):
            await repository.get(uuid4())
        with pytest.raises(InstanceNotFoundError):
            await repository.create_and_request_update(uuid4(), initial_payload, cipher)
        with pytest.raises(InstanceActionConflictError):
            await repository.create_and_request_update(instance_id, initial_payload, cipher)

    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        stored.status = InstanceStatus.ERROR
        await session.commit()
    async with session_maker() as session:
        with pytest.raises(InstanceActionConflictError):
            await InstanceKubernetesRepository(session).create_and_request_update(
                instance_id,
                initial_payload,
                cipher,
            )

    await mark_instance_idle(session_maker, instance_id, expected_action="creating")
    async with session_maker() as session:
        repository = InstanceKubernetesRepository(session)
        created = await repository.create_and_request_update(instance_id, initial_payload, cipher)
        first_envelope = created.token_enc
        assert await repository.get(instance_id) is created

    await mark_instance_idle(session_maker, instance_id, expected_action="updating")
    async with session_maker() as session:
        with pytest.raises(InstanceKubernetesAlreadyConfiguredError):
            await InstanceKubernetesRepository(session).create_and_request_update(
                instance_id,
                initial_payload,
                cipher,
            )

    immutable_payload = InstanceKubernetesUpdate.model_validate(
        provider_update_payload(host="https://immutable.example.test:6443")
    )
    async with session_maker() as session:
        with pytest.raises(InstanceKubernetesImmutableFieldError):
            await InstanceKubernetesRepository(session).update_and_request_update(
                instance_id,
                immutable_payload,
                cipher,
            )

    rotated_payload = InstanceKubernetesUpdate.model_validate(
        provider_update_payload(token="rotated-kubernetes-secret")  # noqa: S106
    )
    async with session_maker() as session:
        rotated = await InstanceKubernetesRepository(session).update_and_request_update(
            instance_id,
            rotated_payload,
            cipher,
        )
        assert rotated.token_enc != first_envelope
        assert (
            cipher.decrypt(rotated.token_enc, instance_id).get_secret_value()
            == "rotated-kubernetes-secret"
        )
