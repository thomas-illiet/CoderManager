"""Workspace and allowed Docker image API behavior tests."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coder_manager.models import (
    Instance,
    InstanceStatus,
    MemberStatus,
    TemplateDeployment,
    TemplateDeploymentStatus,
    Workspace,
    WorkspaceStatus,
)
from coder_manager.repositories import (
    InvalidWorkspaceActionError,
    MemberRepository,
    TemplateImageRepository,
    TemplateRepository,
    WorkspaceActionConflictError,
    WorkspaceNotFoundError,
    WorkspaceRepository,
)
from coder_manager.schemas import (
    TemplateImageCreate,
    TemplateUpdate,
    WorkspaceCreate,
    WorkspaceListQuery,
    WorkspaceUpdate,
)


async def create_instance(
    client: AsyncClient,
    application: str,
    *,
    environment: str = "development",
) -> dict[str, object]:
    """Create an instance through the API."""

    response = await client.post(
        "/api/v1/instances",
        json={
            "application": application,
            "environment": environment,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["resource"]


async def set_instance_status(
    session_maker: async_sessionmaker[AsyncSession],
    instance_id: object,
    *,
    status: InstanceStatus = InstanceStatus.SUCCESS,
    action: str = "creating",
) -> None:
    """Move an instance to a worker-controlled state."""

    async with session_maker() as session:
        instance = await session.get(Instance, UUID(str(instance_id)))
        assert instance is not None
        instance.action = action
        instance.status = status
        await session.commit()


async def create_member(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    instance_id: object,
    *,
    username: str = "alice",
    ready: bool = True,
) -> dict[str, object]:
    """Create an instance member and optionally complete provisioning."""

    response = await client.post(
        f"/api/v1/instances/{instance_id}/members",
        json={"username": username, "role": "user"},
    )
    assert response.status_code == 201, response.text
    member = response.json()["resource"]
    if ready:
        async with session_maker() as session:
            await MemberRepository(session).update_action(
                UUID(str(member["id"])),
                expected_action="creating",
                action="creating",
                status=MemberStatus.SUCCESS,
            )
    await set_instance_status(
        session_maker,
        instance_id,
        action="updating",
        status=InstanceStatus.SUCCESS,
    )
    return member


async def create_template(
    client: AsyncClient,
    *,
    display_name: str = "Python",
    scope: str = "global",
    application: str | None = None,
    modules: list[str] | None = None,
) -> dict[str, object]:
    """Create a resource-bounded template."""

    response = await client.post(
        "/api/v1/templates",
        json={
            "display_name": display_name,
            "name": display_name.lower().replace(" ", "-"),
            "scope": scope,
            "application": application,
            "git_url": "https://git.example.com/template.git",
            "source_path": ".",
            "branch": "main",
            "modules": modules or ["code-server", "git-config"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_image(
    client: AsyncClient,
    template_id: object,
    *,
    name: str = "company/python",
    version: str = "3.13",
) -> dict[str, object]:
    """Allow an image on a template."""

    response = await client.post(
        f"/api/v1/templates/{template_id}/images",
        json={"registry": " Registry.Example.COM ", "name": name, "version": version},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_ready_context(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    """Create a ready instance, owner, template, and image."""

    instance = await create_instance(client, "APPLICATION 1")
    await set_instance_status(session_maker, instance["id"])
    member = await create_member(client, session_maker, instance["id"])
    template = await create_template(client)
    image = await create_image(client, template["id"])
    async with session_maker() as session:
        session.add(
            TemplateDeployment(
                template_id=UUID(str(template["id"])),
                instance_id=UUID(str(instance["id"])),
                coder_template_id=uuid4(),
                target_commit="a" * 40,
                applied_commit="a" * 40,
                target_system_parameter_revision=0,
                applied_system_parameter_revision=0,
                status=TemplateDeploymentStatus.SUCCESS,
            )
        )
        await session.commit()
    return instance, member, template, image


def workspace_payload(
    instance: dict[str, object],
    member: dict[str, object],
    template: dict[str, object],
    image: dict[str, object],
    **overrides: object,
) -> dict[str, object]:
    """Build a valid workspace creation payload."""

    payload: dict[str, object] = {
        "name": "development",
        "instance_id": instance["id"],
        "template_id": template["id"],
        "member_id": member["id"],
        "image_id": image["id"],
        "modules": ["code-server"],
        "parameters": {},
    }
    payload.update(overrides)
    return payload


async def set_workspace_status(
    session_maker: async_sessionmaker[AsyncSession],
    workspace_id: object,
    *,
    expected_action: str = "creating",
    action: str | None = None,
    status: WorkspaceStatus = WorkspaceStatus.SUCCESS,
) -> None:
    """Move a workspace to a worker-controlled state."""

    async with session_maker() as session:
        await WorkspaceRepository(session).update_action(
            UUID(str(workspace_id)),
            expected_action=expected_action,
            action=action or expected_action,
            status=status,
        )


async def test_template_image_crud_normalization_and_pagination(client: AsyncClient) -> None:
    """Verify the template image crud normalization and pagination scenario."""

    template = await create_template(client)
    first = await create_image(client, template["id"], name="Company/Python", version="3.13")
    second = await create_image(client, template["id"], name="company/go", version="1.24")

    assert first["registry"] == "registry.example.com"
    assert first["name"] == "company/python"
    assert datetime.fromisoformat(str(first["created_at"]))

    page = await client.get(
        f"/api/v1/templates/{template['id']}/images",
        params={"page": 1, "page_size": 1},
    )
    fetched = await client.get(f"/api/v1/templates/{template['id']}/images/{first['id']}")
    duplicate = await client.post(
        f"/api/v1/templates/{template['id']}/images",
        json={
            "registry": "REGISTRY.EXAMPLE.COM",
            "name": "COMPANY/PYTHON",
            "version": "3.13",
        },
    )

    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert page.json()["pages"] == 2
    assert fetched.json() == first
    assert duplicate.status_code == 409

    deleted = await client.delete(f"/api/v1/templates/{template['id']}/images/{second['id']}")
    assert deleted.status_code == 204
    missing = await client.get(f"/api/v1/templates/{template['id']}/images/{second['id']}")
    assert missing.status_code == 404


async def test_template_image_missing_and_validation_contract(client: AsyncClient) -> None:
    """Verify the template image missing and validation contract scenario."""

    missing_template = uuid4()
    missing_image = uuid4()
    payload = {"registry": "registry.example.com", "name": "company/python", "version": "1"}
    responses = [
        await client.get(f"/api/v1/templates/{missing_template}/images"),
        await client.post(f"/api/v1/templates/{missing_template}/images", json=payload),
        await client.delete(f"/api/v1/templates/{missing_template}/images/{missing_image}"),
    ]
    assert all(response.status_code == 404 for response in responses)

    template = await create_template(client)
    missing_delete = await client.delete(
        f"/api/v1/templates/{template['id']}/images/{missing_image}"
    )
    invalid = await client.post(
        f"/api/v1/templates/{template['id']}/images",
        json={"registry": " ", "name": "image", "version": "1"},
    )
    immutable = await client.put(
        f"/api/v1/templates/{template['id']}/images/{missing_image}", json=payload
    )
    assert missing_delete.status_code == 404
    assert invalid.status_code == 422
    assert immutable.status_code == 405


async def test_workspace_crud_filters_and_image_change(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the workspace crud filters and image change scenario."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    second_image = await create_image(client, template["id"], version="3.14")
    created_response = await client.post(
        "/api/v1/workspaces", json=workspace_payload(instance, member, template, image)
    )
    assert created_response.status_code == 201
    created = created_response.json()["resource"]
    assert created["action"] == "creating"
    assert created["status"] == "pending"
    assert created["parameters"] == {}

    fetched = await client.get(f"/api/v1/workspaces/{created['id']}")
    filtered = await client.get(
        "/api/v1/workspaces",
        params={
            "instance_id": instance["id"],
            "template_id": template["id"],
            "member_id": member["id"],
            "image_id": image["id"],
            "status": "pending",
            "name": "VELOP",
        },
    )
    assert fetched.json() == created
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1

    blocked = await client.put(
        f"/api/v1/workspaces/{created['id']}",
        json={
            "name": "development",
            "image_id": image["id"],
            "modules": [],
            "parameters": {},
        },
    )
    assert blocked.status_code == 409
    await set_workspace_status(session_maker, created["id"])

    ready = (await client.get(f"/api/v1/workspaces/{created['id']}")).json()
    no_op = await client.put(
        f"/api/v1/workspaces/{created['id']}",
        json={
            "name": ready["name"],
            "image_id": ready["image_id"],
            "modules": ready["modules"],
            "parameters": ready["parameters"],
        },
    )
    assert no_op.status_code == 200
    assert no_op.json()["resource"]["updated_at"] == ready["updated_at"]

    updated = await client.put(
        f"/api/v1/workspaces/{created['id']}",
        json={
            "name": "development-updated",
            "image_id": second_image["id"],
            "modules": [],
            "parameters": {},
        },
    )
    assert updated.status_code == 202
    updated_resource = updated.json()["resource"]
    assert updated_resource["image_id"] == second_image["id"]
    assert updated_resource["parameters"] == {}
    assert updated_resource["action"] == "updating"

    await set_workspace_status(
        session_maker,
        created["id"],
        expected_action="updating",
        status=WorkspaceStatus.ERROR,
    )
    retried = await client.put(
        f"/api/v1/workspaces/{created['id']}",
        json={
            "name": "development-updated",
            "image_id": second_image["id"],
            "modules": [],
            "parameters": {},
        },
    )
    assert retried.status_code == 202

    await set_workspace_status(
        session_maker,
        created["id"],
        expected_action="updating",
        status=WorkspaceStatus.ERROR,
    )
    deleted = await client.delete(f"/api/v1/workspaces/{created['id']}")
    assert deleted.status_code == 202
    assert deleted.json()["resource"]["action"] == "deleting"


async def test_failed_parent_instance_blocks_workspace_creation(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Do not let child mutations overwrite a failed instance deletion."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    instance_id = UUID(str(instance["id"]))
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        stored.action = "deleting"
        stored.status = InstanceStatus.ERROR
        await session.commit()

    blocked = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(instance, member, template, image, name="blocked"),
    )

    assert blocked.status_code == 409
    assert blocked.json() == {"detail": "Instance has an action in progress"}


@pytest.mark.parametrize(
    "legacy_field",
    [
        "cpu",
        "ram",
        "disk",
    ],
)
async def test_workspace_creation_rejects_removed_resource_fields(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    legacy_field: str,
) -> None:
    """Reject the removed workspace resource fields through the strict schema."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    payload = workspace_payload(instance, member, template, image)
    payload[legacy_field] = 2
    response = await client.post(
        "/api/v1/workspaces",
        json=payload,
    )
    assert response.status_code == 422


async def test_workspace_creation_rejects_unknown_module(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Keep module compatibility validation after removing resource bounds."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    response = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(instance, member, template, image, modules=["unknown"]),
    )
    assert response.status_code == 422


@pytest.mark.parametrize("legacy_field", ["cpu", "ram", "disk"])
async def test_workspace_update_rejects_removed_resource_fields(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    legacy_field: str,
) -> None:
    """Reject legacy resource fields without changing the workspace."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    created = (
        await client.post(
            "/api/v1/workspaces", json=workspace_payload(instance, member, template, image)
        )
    ).json()["resource"]
    await set_workspace_status(session_maker, created["id"])
    original = (await client.get(f"/api/v1/workspaces/{created['id']}")).json()

    payload: dict[str, object] = {
        "name": "must-not-change",
        "image_id": image["id"],
        "modules": [],
        "parameters": {},
    }
    payload[legacy_field] = 2
    rejected = await client.put(
        f"/api/v1/workspaces/{created['id']}",
        json=payload,
    )
    assert rejected.status_code == 422
    assert (await client.get(f"/api/v1/workspaces/{created['id']}")).json() == original


async def test_workspace_user_parameter_defaults_immutability_and_history(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Resolve defaults, fence immutable values, and preserve deleted snapshots."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    parameter_url = f"/api/v1/templates/{template['id']}/parameters"
    project = await client.post(
        parameter_url,
        json={
            "type": "user",
            "name": "project_name",
            "display_name": "Project name",
            "required": True,
            "mutable": False,
            "default_value": "demo",
        },
    )
    region = await client.post(
        parameter_url,
        json={
            "type": "user",
            "name": "region",
            "display_name": "Region",
            "required": False,
            "mutable": True,
            "default_value": "eu",
        },
    )
    assert project.status_code == 201
    assert region.status_code == 201

    created_response = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(
            instance,
            member,
            template,
            image,
            parameters={"project_name": "alpha"},
        ),
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()["resource"]
    assert created["parameters"] == {"project_name": "alpha", "region": "eu"}
    await set_workspace_status(session_maker, created["id"])

    immutable = await client.put(
        f"/api/v1/workspaces/{created['id']}",
        json={
            "name": created["name"],
            "image_id": image["id"],
            "modules": [],
            "parameters": {"project_name": "beta", "region": "eu"},
        },
    )
    unknown = await client.put(
        f"/api/v1/workspaces/{created['id']}",
        json={
            "name": created["name"],
            "image_id": image["id"],
            "modules": [],
            "parameters": {
                "project_name": "alpha",
                "region": "eu",
                "unknown": "value",
            },
        },
    )
    assert immutable.status_code == 409
    assert unknown.status_code == 422

    mutable = await client.put(
        f"/api/v1/workspaces/{created['id']}",
        json={
            "name": created["name"],
            "image_id": image["id"],
            "modules": [],
            "parameters": {"project_name": "alpha", "region": "us"},
        },
    )
    assert mutable.status_code == 202, mutable.text
    assert mutable.json()["resource"]["parameters"] == {
        "project_name": "alpha",
        "region": "us",
    }
    await set_workspace_status(
        session_maker,
        created["id"],
        expected_action="updating",
    )

    deleted = await client.delete(f"{parameter_url}/{project.json()['id']}")
    assert deleted.status_code == 204
    historical = await client.put(
        f"/api/v1/workspaces/{created['id']}",
        json={
            "name": created["name"],
            "image_id": image["id"],
            "modules": [],
            "parameters": {"project_name": "alpha", "region": "apac"},
        },
    )
    assert historical.status_code == 202, historical.text
    assert historical.json()["resource"]["parameters"] == {
        "project_name": "alpha",
        "region": "apac",
    }


async def test_workspace_required_parameter_and_name_contract(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Reject missing required values and names outside Coder's contract."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    parameter = await client.post(
        f"/api/v1/templates/{template['id']}/parameters",
        json={
            "type": "user",
            "name": "project_name",
            "display_name": "Project name",
            "required": True,
            "mutable": True,
            "default_value": None,
        },
    )
    assert parameter.status_code == 201
    missing = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(instance, member, template, image),
    )
    invalid_character = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(
            instance,
            member,
            template,
            image,
            name="invalid_name",
            parameters={"project_name": "demo"},
        ),
    )
    too_long = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(
            instance,
            member,
            template,
            image,
            name="a" * 33,
            parameters={"project_name": "demo"},
        ),
    )
    assert missing.status_code == 422
    assert invalid_character.status_code == 422
    assert too_long.status_code == 422


async def test_workspace_relationship_validation_and_name_uniqueness(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the workspace relationship validation and name uniqueness scenario."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    pending_member = await create_member(
        client, session_maker, instance["id"], username="pending", ready=False
    )
    other_template = await create_template(client, display_name="Go")
    other_image = await create_image(client, other_template["id"], name="company/go")

    pending_owner = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(instance, pending_member, template, image),
    )
    wrong_image = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(instance, member, template, other_image),
    )
    assert pending_owner.status_code == 409
    assert wrong_image.status_code == 422

    created = await client.post(
        "/api/v1/workspaces", json=workspace_payload(instance, member, template, image)
    )
    duplicate = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(instance, member, template, image, name="DEVELOPMENT"),
    )
    assert created.status_code == 201
    assert duplicate.status_code == 409

    created_resource = created.json()["resource"]
    await set_workspace_status(session_maker, created_resource["id"])
    immutable = await client.put(
        f"/api/v1/workspaces/{created_resource['id']}",
        json={
            "name": "development",
            "image_id": image["id"],
            "modules": [],
            "parameters": {},
            "instance_id": instance["id"],
        },
    )
    assert immutable.status_code == 422


async def test_instance_and_workspace_processing_blocks_mutations_but_not_reads(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the instance and workspace processing blocks mutations but not reads scenario."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    created = (
        await client.post(
            "/api/v1/workspaces", json=workspace_payload(instance, member, template, image)
        )
    ).json()["resource"]
    read_while_pending = await client.get(f"/api/v1/workspaces/{created['id']}")
    delete_pending = await client.delete(f"/api/v1/workspaces/{created['id']}")
    assert read_while_pending.status_code == 200
    assert delete_pending.status_code == 409

    await set_workspace_status(session_maker, created["id"])
    await set_instance_status(
        session_maker,
        instance["id"],
        action="synchronizing",
        status=InstanceStatus.RUNNING,
    )
    blocked_create = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(instance, member, template, image, name="second"),
    )
    blocked_update = await client.put(
        f"/api/v1/workspaces/{created['id']}",
        json={"name": "new", "image_id": image["id"], "modules": [], "parameters": {}},
    )
    blocked_delete = await client.delete(f"/api/v1/workspaces/{created['id']}")
    assert {blocked_create.status_code, blocked_update.status_code, blocked_delete.status_code} == {
        409
    }


async def test_template_image_member_deletion_and_template_changes_are_protected(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the template image member deletion and template changes are protected scenario."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    created = (
        await client.post(
            "/api/v1/workspaces", json=workspace_payload(instance, member, template, image)
        )
    ).json()["resource"]
    await set_workspace_status(session_maker, created["id"])

    image_delete = await client.delete(f"/api/v1/templates/{template['id']}/images/{image['id']}")
    member_delete = await client.delete(
        f"/api/v1/instances/{instance['id']}/members/{member['id']}"
    )
    template_delete = await client.delete(f"/api/v1/templates/{template['id']}")
    assert image_delete.status_code == 409
    assert member_delete.status_code == 409
    assert template_delete.status_code == 409

    incompatible = await client.put(
        f"/api/v1/templates/{template['id']}",
        json={
            "display_name": template["display_name"],
            "git_url": template["git_url"],
            "source_path": template["source_path"],
            "branch": template["branch"],
            "modules": ["git-config"],
        },
    )
    assert incompatible.status_code == 409

    compatible = await client.put(
        f"/api/v1/templates/{template['id']}",
        json={
            "display_name": "Python updated",
            "git_url": template["git_url"],
            "source_path": template["source_path"],
            "branch": "release/v2",
            "modules": template["modules"],
        },
    )
    assert compatible.status_code == 200


async def test_workspace_missing_resources_and_cross_scope_template(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the workspace missing resources and cross scope template scenario."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    fields = (
        ("instance_id", uuid4()),
        ("template_id", uuid4()),
        ("member_id", uuid4()),
        ("image_id", uuid4()),
    )
    for field, value in fields:
        payload = workspace_payload(instance, member, template, image)
        payload[field] = str(value)
        response = await client.post("/api/v1/workspaces", json=payload)
        assert response.status_code == 404

    scoped = await create_template(
        client,
        display_name="Scoped",
        scope="application",
        application="APPLICATION 2",
    )
    scoped_image = await create_image(client, scoped["id"])
    unavailable = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(instance, member, scoped, scoped_image),
    )
    assert unavailable.status_code == 422
    assert (await client.get(f"/api/v1/workspaces/{uuid4()}")).status_code == 404
    assert (await client.delete(f"/api/v1/workspaces/{uuid4()}")).status_code == 404


async def test_internal_workspace_action_validation(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the internal workspace action validation scenario."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    created = (
        await client.post(
            "/api/v1/workspaces", json=workspace_payload(instance, member, template, image)
        )
    ).json()["resource"]
    workspace_id = UUID(str(created["id"]))

    async with session_maker() as session:
        repository = WorkspaceRepository(session)
        running = await repository.update_action(
            workspace_id,
            expected_action="creating",
            action="provisioning",
            status=WorkspaceStatus.RUNNING,
        )
        assert running.action == "provisioning"
        with pytest.raises(WorkspaceActionConflictError):
            await repository.update_action(
                workspace_id,
                expected_action="creating",
                action="creating",
                status=WorkspaceStatus.ERROR,
            )
        with pytest.raises(InvalidWorkspaceActionError):
            await repository.update_action(
                workspace_id,
                expected_action="provisioning",
                action=" ",
                status=WorkspaceStatus.ERROR,
            )
        with pytest.raises(InvalidWorkspaceActionError):
            await repository.update_action(
                workspace_id,
                expected_action="provisioning",
                action="a" * 256,
                status=WorkspaceStatus.ERROR,
            )
        with pytest.raises(WorkspaceNotFoundError):
            await repository.update_action(
                uuid4(),
                expected_action="creating",
                action="creating",
                status=WorkspaceStatus.SUCCESS,
            )


async def test_workspace_updated_at_changes_on_real_update(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the workspace updated at changes on real update scenario."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    created = (
        await client.post(
            "/api/v1/workspaces", json=workspace_payload(instance, member, template, image)
        )
    ).json()["resource"]
    await set_workspace_status(session_maker, created["id"])
    old_timestamp = datetime.now(UTC) - timedelta(days=1)
    async with session_maker() as session:
        workspace = await session.get(Workspace, UUID(str(created["id"])))
        assert workspace is not None
        workspace.updated_at = old_timestamp
        await session.commit()

    updated = await client.put(
        f"/api/v1/workspaces/{created['id']}",
        json={
            "name": "updated",
            "image_id": image["id"],
            "modules": [],
            "parameters": {},
        },
    )
    assert updated.status_code == 202
    changed_at = datetime.fromisoformat(updated.json()["resource"]["updated_at"]).replace(
        tzinfo=UTC
    )
    assert changed_at > datetime.now(UTC) - timedelta(hours=1)


async def test_repositories_exercise_direct_successful_lifecycle(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the repositories exercise direct successful lifecycle scenario."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    template_id = UUID(str(template["id"]))

    async with session_maker() as session:
        template_repository = TemplateRepository(session)
        templates, total = await template_repository.list(
            page=1,
            page_size=20,
            application=None,
            scope=None,
            display_name="Python",
        )
        assert total == 1
        assert templates[0].id == template_id
        stored_template = await template_repository.get(template_id)
        assert stored_template is not None
        previous_updated_at = stored_template.updated_at
        unchanged_template = await template_repository.update(
            template_id,
            TemplateUpdate(
                display_name="Python",
                git_url="https://git.example.com/template.git",
                source_path=".",
                branch="main",
                modules=["code-server", "git-config"],
            ),
        )
        assert unchanged_template.updated_at == previous_updated_at

    async with session_maker() as session:
        image_repository = TemplateImageRepository(session)
        images, total = await image_repository.list(template_id, page=1, page_size=20)
        assert total == 1
        assert images[0].id == UUID(str(image["id"]))
        assert await image_repository.get(template_id, images[0].id) is not None
        disposable = await image_repository.create(
            template_id,
            TemplateImageCreate(
                registry="docker.io",
                name="company/disposable",
                version="1",
            ),
        )
        await image_repository.delete(template_id, disposable.id)

    create_payload = WorkspaceCreate.model_validate(
        workspace_payload(instance, member, template, image, name="repository-workspace")
    )
    async with session_maker() as session:
        repository = WorkspaceRepository(session)
        workspace = await repository.create(create_payload)
        workspace_id = workspace.id
        page, total = await repository.list_page(
            WorkspaceListQuery(page=1, page_size=20, instance_id=workspace.instance_id)
        )
        assert total == 1
        assert page[0].id == workspace_id
        assert await repository.get(workspace_id) is not None
        await repository.update_action(
            workspace_id,
            expected_action="creating",
            action="creating",
            status=WorkspaceStatus.SUCCESS,
        )
        updated, changed = await repository.update(
            workspace_id,
            WorkspaceUpdate(
                name="repository-updated",
                image_id=UUID(str(image["id"])),
                modules=[],
                parameters={},
            ),
        )
        assert changed is True
        assert updated.parameters == {}
        await repository.update_action(
            workspace_id,
            expected_action="updating",
            action="updating",
            status=WorkspaceStatus.SUCCESS,
        )
        deleted = await repository.request_deletion(workspace_id)
        assert deleted.action == "deleting"

    disposable_template = await create_template(client, display_name="Disposable")
    async with session_maker() as session:
        await TemplateRepository(session).delete(UUID(str(disposable_template["id"])))
