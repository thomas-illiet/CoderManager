"""Template parameter CRUD, encryption, and revision tests."""

# ruff: noqa: SLF001

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coder_manager.config import Settings, get_settings
from coder_manager.crypto import (
    CryptoConfigurationError,
    TemplateParameterCipher,
    TemplateParameterDecryptionError,
)
from coder_manager.main import app
from coder_manager.models import (
    Template,
    TemplateParameterScope,
    TemplateParameterSystemValue,
    TemplateParameterType,
    TemplateScope,
    TemplateSyncStatus,
)
from coder_manager.repositories import (
    TemplateParameterImmutableFieldError,
    TemplateParameterRepository,
)
from coder_manager.schemas import (
    SystemTemplateParameterCreate,
    SystemTemplateParameterUpdate,
    UserTemplateParameterCreate,
    UserTemplateParameterUpdate,
)
from tests.conftest import TEST_CRYPTO_KEY


async def create_template(client: AsyncClient, name: str = "python") -> dict[str, object]:
    """Create one template for parameter tests."""

    response = await client.post(
        "/api/v1/templates",
        json={
            "display_name": name.title(),
            "name": name,
            "scope": "global",
            "application": None,
            "git_url": "https://git.example.com/template.git",
            "source_path": ".",
            "branch": "main",
            "modules": [],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def template_model() -> Template:
    """Build an unattached template for synchronous definition logic tests."""

    return Template(
        id=uuid4(),
        display_name="Python",
        name="python",
        scope=TemplateScope.GLOBAL,
        application=None,
        git_url="https://git.example.com/template.git",
        source_path=".",
        branch="main",
        modules=[],
        system_parameter_revision=0,
    )


def test_parameter_definition_logic_without_database_io() -> None:
    """Exercise user/system construction, immutable fields, and secret rotation."""

    template = template_model()
    user, user_values = TemplateParameterRepository._new_parameter(
        template,
        UserTemplateParameterCreate(
            type=TemplateParameterType.USER,
            name="project_name",
            display_name="Project",
            required=False,
            mutable=True,
            default_value="demo",
        ),
        None,
    )
    assert user_values == ()
    assert user.default_value == "demo"
    assert user.scope is None
    TemplateParameterRepository._apply_update(
        template,
        user,
        UserTemplateParameterUpdate(
            type=TemplateParameterType.USER,
            display_name="Project name",
            description="Updated",
            required=True,
            mutable=False,
            default_value=None,
        ),
        None,
    )
    assert user.required is True
    assert user.mutable is False
    assert user.default_value is None

    global_create = SystemTemplateParameterCreate(
        type=TemplateParameterType.SYSTEM,
        name="token",
        display_name="Token",
        scope=TemplateParameterScope.GLOBAL,
        value="secret-v1",
    )
    with pytest.raises(RuntimeError, match="encryption is required"):
        TemplateParameterRepository._new_parameter(template, global_create, None)

    cipher = TemplateParameterCipher(SecretStr(TEST_CRYPTO_KEY))
    system, system_values = TemplateParameterRepository._new_parameter(
        template,
        global_create,
        cipher,
    )
    system.system_values = list(system_values)
    assert template.system_parameter_revision == 1
    assert (
        cipher.decrypt(
            system.system_values[0].value_enc,
            system.id,
            "global",
        )
        == "secret-v1"
    )

    original_updated_at = system.updated_at
    TemplateParameterRepository._apply_update(
        template,
        system,
        SystemTemplateParameterUpdate(
            type=TemplateParameterType.SYSTEM,
            display_name="Token",
            scope=TemplateParameterScope.GLOBAL,
        ),
        cipher,
    )
    assert template.system_parameter_revision == 1
    assert system.updated_at == original_updated_at

    TemplateParameterRepository._apply_update(
        template,
        system,
        SystemTemplateParameterUpdate(
            type=TemplateParameterType.SYSTEM,
            display_name="Rotated token",
            scope=TemplateParameterScope.GLOBAL,
            value="secret-v2",
        ),
        cipher,
    )
    assert template.system_parameter_revision == 2
    assert (
        cipher.decrypt(
            system.system_values[0].value_enc,
            system.id,
            "global",
        )
        == "secret-v2"
    )
    TemplateParameterRepository._apply_update(
        template,
        system,
        SystemTemplateParameterUpdate(
            type=TemplateParameterType.SYSTEM,
            display_name="Rotated token",
            scope=TemplateParameterScope.GLOBAL,
            value="secret-v2",
        ),
        cipher,
    )
    assert template.system_parameter_revision == 2

    environment, environment_values = TemplateParameterRepository._new_parameter(
        template,
        SystemTemplateParameterCreate(
            type=TemplateParameterType.SYSTEM,
            name="environment_token",
            display_name="Environment token",
            scope=TemplateParameterScope.ENVIRONMENT,
            values={
                "development": "dev",
                "staging": "stg",
                "production": "prd",
            },
        ),
        cipher,
    )
    assert environment.scope is TemplateParameterScope.ENVIRONMENT
    assert {value.target.value for value in environment_values} == {
        "development",
        "staging",
        "production",
    }
    assert template.system_parameter_revision == 3

    with pytest.raises(RuntimeError, match="encryption is required"):
        TemplateParameterRepository._apply_update(
            template,
            system,
            SystemTemplateParameterUpdate(
                type=TemplateParameterType.SYSTEM,
                display_name="Token",
                scope=TemplateParameterScope.GLOBAL,
                value="secret-v3",
            ),
            None,
        )
    with pytest.raises(TemplateParameterImmutableFieldError):
        TemplateParameterRepository._apply_update(
            template,
            system,
            UserTemplateParameterUpdate(
                type=TemplateParameterType.USER,
                display_name="Token",
                required=False,
                mutable=True,
            ),
            None,
        )
    with pytest.raises(TemplateParameterImmutableFieldError):
        TemplateParameterRepository._apply_update(
            template,
            system,
            SystemTemplateParameterUpdate(
                type=TemplateParameterType.SYSTEM,
                display_name="Token",
                scope=TemplateParameterScope.ENVIRONMENT,
            ),
            cipher,
        )


async def test_parameter_crud_pagination_redaction_and_encryption(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Store user and system variants while exposing only configured markers."""

    template = await create_template(client)
    template_id = str(template["id"])
    user = await client.post(
        f"/api/v1/templates/{template_id}/parameters",
        json={
            "type": "user",
            "name": "project_name",
            "display_name": "Project name",
            "description": "Workspace project",
            "required": True,
            "mutable": False,
            "default_value": None,
        },
    )
    assert user.status_code == 201, user.text
    assert user.json()["name"] == "project_name"
    assert user.json()["required"] is True
    assert user.json()["value_configured"] is None

    global_system = await client.post(
        f"/api/v1/templates/{template_id}/parameters",
        json={
            "type": "system",
            "name": "global_token",
            "display_name": "Global token",
            "scope": "global",
            "value": "global-secret",
        },
    )
    assert global_system.status_code == 201, global_system.text
    assert global_system.headers["cache-control"] == "no-store"
    assert global_system.json()["value_configured"] is True
    assert "value" not in global_system.json()
    assert "global-secret" not in global_system.text

    environment_system = await client.post(
        f"/api/v1/templates/{template_id}/parameters",
        json={
            "type": "system",
            "name": "environment_token",
            "display_name": "Environment token",
            "scope": "environment",
            "values": {
                "development": "dev-secret",
                "staging": "staging-secret",
                "production": "production-secret",
            },
        },
    )
    assert environment_system.status_code == 201, environment_system.text
    assert environment_system.json()["values_configured"] == {
        "development": True,
        "staging": True,
        "production": True,
    }
    assert "secret" not in environment_system.text

    first_page = await client.get(
        f"/api/v1/templates/{template_id}/parameters",
        params={"page": 1, "page_size": 2},
    )
    second_page = await client.get(
        f"/api/v1/templates/{template_id}/parameters",
        params={"page": 2, "page_size": 2},
    )
    assert first_page.status_code == 200
    assert first_page.json()["total"] == 3
    assert first_page.json()["pages"] == 2
    assert len(first_page.json()["items"]) == 2
    assert len(second_page.json()["items"]) == 1

    cipher = TemplateParameterCipher(SecretStr(TEST_CRYPTO_KEY))
    async with session_maker() as session:
        values = list(
            await session.scalars(
                select(TemplateParameterSystemValue).order_by(
                    TemplateParameterSystemValue.parameter_id,
                    TemplateParameterSystemValue.target,
                )
            )
        )
        assert len(values) == 4
        assert all(b"secret" not in value.value_enc for value in values)
        plaintext = {
            value.target.value: cipher.decrypt(
                value.value_enc,
                value.parameter_id,
                value.target.value,
            )
            for value in values
            if value.parameter_id == UUID(str(environment_system.json()["id"]))
        }
        assert plaintext == {
            "development": "dev-secret",
            "staging": "staging-secret",
            "production": "production-secret",
        }


async def test_parameter_conflicts_immutability_and_effective_revision(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Enforce stable identity while advancing only effective system changes."""

    template = await create_template(client)
    template_id = str(template["id"])
    created = await client.post(
        f"/api/v1/templates/{template_id}/parameters",
        json={
            "type": "system",
            "name": "registry_token",
            "display_name": "Registry token",
            "scope": "global",
            "value": "secret-v1",
        },
    )
    assert created.status_code == 201
    parameter_id = created.json()["id"]

    duplicate = await client.post(
        f"/api/v1/templates/{template_id}/parameters",
        json={
            "type": "user",
            "name": "registry_token",
            "display_name": "Duplicate",
            "required": False,
            "mutable": True,
        },
    )
    assert duplicate.status_code == 409

    async def revision() -> int:
        """Return the persisted system parameter revision."""

        async with session_maker() as session:
            stored = await session.get(Template, UUID(template_id))
            assert stored is not None
            return stored.system_parameter_revision

    assert await revision() == 1
    unchanged = await client.put(
        f"/api/v1/templates/{template_id}/parameters/{parameter_id}",
        json={
            "type": "system",
            "display_name": "Renamed token",
            "scope": "global",
        },
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["value_configured"] is True
    assert await revision() == 1

    same_secret = await client.put(
        f"/api/v1/templates/{template_id}/parameters/{parameter_id}",
        json={
            "type": "system",
            "display_name": "Renamed token",
            "scope": "global",
            "value": "secret-v1",
        },
    )
    assert same_secret.status_code == 200
    assert await revision() == 1

    rotated = await client.put(
        f"/api/v1/templates/{template_id}/parameters/{parameter_id}",
        json={
            "type": "system",
            "display_name": "Renamed token",
            "scope": "global",
            "value": "secret-v2",
        },
    )
    assert rotated.status_code == 200
    assert await revision() == 2

    changed_scope = await client.put(
        f"/api/v1/templates/{template_id}/parameters/{parameter_id}",
        json={
            "type": "system",
            "display_name": "Renamed token",
            "scope": "environment",
            "values": {
                "development": "a",
                "staging": "b",
                "production": "c",
            },
        },
    )
    changed_type = await client.put(
        f"/api/v1/templates/{template_id}/parameters/{parameter_id}",
        json={
            "type": "user",
            "display_name": "Renamed token",
            "required": False,
            "mutable": True,
        },
    )
    assert changed_scope.status_code == 409
    assert changed_type.status_code == 409

    deleted = await client.delete(f"/api/v1/templates/{template_id}/parameters/{parameter_id}")
    assert deleted.status_code == 204
    assert await revision() == 3


async def test_parameter_schema_rejects_invalid_names_and_incomplete_environments(
    client: AsyncClient,
) -> None:
    """Reject non-lowercase names and environment maps without all targets."""

    template = await create_template(client)
    url = f"/api/v1/templates/{template['id']}/parameters"
    uppercase = await client.post(
        url,
        json={
            "type": "user",
            "name": "ProjectName",
            "display_name": "Project",
            "required": False,
            "mutable": True,
        },
    )
    incomplete = await client.post(
        url,
        json={
            "type": "system",
            "name": "token",
            "display_name": "Token",
            "scope": "environment",
            "values": {"development": "a", "staging": "b"},
        },
    )
    assert uppercase.status_code == 422
    assert incomplete.status_code == 422


async def test_user_parameter_get_update_delete_and_missing_resources(
    client: AsyncClient,
) -> None:
    """Exercise complete user CRUD and constrained missing-resource responses."""

    template = await create_template(client)
    template_id = str(template["id"])
    missing_template_id = uuid4()
    missing_parameter_id = uuid4()
    assert (
        await client.get(f"/api/v1/templates/{missing_template_id}/parameters")
    ).status_code == 404
    missing_create = await client.post(
        f"/api/v1/templates/{missing_template_id}/parameters",
        json={
            "type": "user",
            "name": "missing",
            "display_name": "Missing",
            "required": False,
            "mutable": True,
        },
    )
    assert missing_create.status_code == 404

    created = await client.post(
        f"/api/v1/templates/{template_id}/parameters",
        json={
            "type": "user",
            "name": "project_name",
            "display_name": "Project",
            "required": False,
            "mutable": True,
        },
    )
    assert created.status_code == 201
    parameter_id = created.json()["id"]
    fetched = await client.get(f"/api/v1/templates/{template_id}/parameters/{parameter_id}")
    assert fetched.status_code == 200
    assert fetched.json() == created.json()

    updated = await client.put(
        f"/api/v1/templates/{template_id}/parameters/{parameter_id}",
        json={
            "type": "user",
            "display_name": "Project name",
            "description": "Updated",
            "required": True,
            "mutable": False,
            "default_value": "demo",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Project name"
    assert updated.json()["description"] == "Updated"
    assert updated.json()["default_value"] == "demo"

    missing_get = await client.get(
        f"/api/v1/templates/{template_id}/parameters/{missing_parameter_id}"
    )
    missing_update = await client.put(
        f"/api/v1/templates/{template_id}/parameters/{missing_parameter_id}",
        json={
            "type": "user",
            "display_name": "Missing",
            "required": False,
            "mutable": True,
        },
    )
    missing_delete = await client.delete(
        f"/api/v1/templates/{template_id}/parameters/{missing_parameter_id}"
    )
    missing_template_delete = await client.delete(
        f"/api/v1/templates/{missing_template_id}/parameters/{missing_parameter_id}"
    )
    assert {
        missing_get.status_code,
        missing_update.status_code,
        missing_delete.status_code,
        missing_template_delete.status_code,
    } == {404}

    deleted = await client.delete(f"/api/v1/templates/{template_id}/parameters/{parameter_id}")
    assert deleted.status_code == 204
    assert (
        await client.get(f"/api/v1/templates/{template_id}/parameters/{parameter_id}")
    ).status_code == 404


async def test_parameter_mutations_are_blocked_during_template_sync(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Return conflicts for create, update, and delete while sync owns the snapshot."""

    template = await create_template(client)
    template_id = UUID(str(template["id"]))
    created = await client.post(
        f"/api/v1/templates/{template_id}/parameters",
        json={
            "type": "user",
            "name": "project_name",
            "display_name": "Project",
            "required": False,
            "mutable": True,
        },
    )
    assert created.status_code == 201
    async with session_maker() as session:
        stored = await session.get(Template, template_id)
        assert stored is not None
        stored.sync_status = TemplateSyncStatus.PENDING
        await session.commit()

    create_conflict = await client.post(
        f"/api/v1/templates/{template_id}/parameters",
        json={
            "type": "user",
            "name": "region",
            "display_name": "Region",
            "required": False,
            "mutable": True,
        },
    )
    update_conflict = await client.put(
        f"/api/v1/templates/{template_id}/parameters/{created.json()['id']}",
        json={
            "type": "user",
            "display_name": "Project",
            "required": False,
            "mutable": True,
        },
    )
    delete_conflict = await client.delete(
        f"/api/v1/templates/{template_id}/parameters/{created.json()['id']}"
    )
    assert {
        create_conflict.status_code,
        update_conflict.status_code,
        delete_conflict.status_code,
    } == {409}


async def test_system_parameter_requires_configured_encryption(
    client: AsyncClient,
) -> None:
    """Return a redacted service error when the AES key is unavailable."""

    template = await create_template(client)
    app.dependency_overrides[get_settings] = lambda: Settings(crypto_key=None)
    response = await client.post(
        f"/api/v1/templates/{template['id']}/parameters",
        json={
            "type": "system",
            "name": "token",
            "display_name": "Token",
            "scope": "global",
            "value": "secret",
        },
    )
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "Template parameter encryption is not configured"}
    assert "secret" not in response.text


async def test_parameter_update_rejects_tampered_envelope_without_secret_leak(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Return a redacted service error when stored authenticated data is altered."""

    template = await create_template(client)
    created = await client.post(
        f"/api/v1/templates/{template['id']}/parameters",
        json={
            "type": "system",
            "name": "token",
            "display_name": "Token",
            "scope": "global",
            "value": "never-return-this-secret",
        },
    )
    assert created.status_code == 201
    parameter_id = UUID(created.json()["id"])
    async with session_maker() as session:
        value = await session.scalar(
            select(TemplateParameterSystemValue).where(
                TemplateParameterSystemValue.parameter_id == parameter_id
            )
        )
        assert value is not None
        value.value_enc = b"altered"
        await session.commit()

    response = await client.put(
        f"/api/v1/templates/{template['id']}/parameters/{parameter_id}",
        json={
            "type": "system",
            "display_name": "Token",
            "scope": "global",
            "value": "never-return-this-secret",
        },
    )
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "Template parameter value cannot be decrypted"}
    assert "never-return-this-secret" not in response.text


def test_template_parameter_envelope_is_bound_to_parameter_and_target() -> None:
    """Reject altered envelopes and associated-data substitutions."""

    cipher = TemplateParameterCipher(SecretStr(TEST_CRYPTO_KEY))
    first = UUID("00000000-0000-0000-0000-000000000001")
    second = UUID("00000000-0000-0000-0000-000000000002")
    envelope = cipher.encrypt("secret", first, "development")
    altered = envelope[:-1] + bytes((envelope[-1] ^ 1,))

    for candidate_id, target, candidate in (
        (first, "development", altered),
        (second, "development", envelope),
        (first, "staging", envelope),
    ):
        with pytest.raises(TemplateParameterDecryptionError):
            cipher.decrypt(candidate, candidate_id, target)


def test_template_parameter_cipher_rejects_missing_and_invalid_keys() -> None:
    """Reject absent, malformed, and non-AES-256 encryption keys."""

    for key in (None, SecretStr("not-base64"), SecretStr("YQ==")):
        with pytest.raises(CryptoConfigurationError):
            TemplateParameterCipher(key)
