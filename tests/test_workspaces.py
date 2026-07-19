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
    Template,
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

LIMITS = {
    "min_cpu_count": 1,
    "max_cpu_count": 8,
    "min_ram_gb": 2,
    "max_ram_gb": 32,
    "min_disk_gb": 10,
    "max_disk_gb": 100,
}


async def create_application(
    client: AsyncClient,
    *,
    suffix: str = "1",
) -> dict[str, object]:
    """Create and whitelist a business application."""

    response = await client.post(
        "/api/v1/applications",
        json={"external_id": f"app-{suffix}", "name": f"Application {suffix}"},
    )
    assert response.status_code == 201
    application = response.json()
    whitelisted = await client.post(f"/api/v1/applications/{application['id']}/whitelist")
    assert whitelisted.status_code == 204
    return application


async def create_instance(
    client: AsyncClient,
    application_id: object,
    *,
    environment: str = "development",
) -> dict[str, object]:
    """Create an instance through the API."""

    response = await client.post(
        "/api/v1/instances",
        json={
            "application_id": str(application_id),
            "region": "emea",
            "environment": environment,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


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
    member = response.json()
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
    name: str = "Python",
    scope: str = "global",
    application_id: object | None = None,
    modules: list[str] | None = None,
) -> dict[str, object]:
    """Create a resource-bounded template."""

    response = await client.post(
        "/api/v1/templates",
        json={
            "name": name,
            "scope": scope,
            "application_id": str(application_id) if application_id is not None else None,
            "git_url": "https://git.example.com/template.git",
            "modules": modules or ["code-server", "git-config"],
            "version": "v1",
            **LIMITS,
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

    application = await create_application(client)
    instance = await create_instance(client, application["id"])
    await set_instance_status(session_maker, instance["id"])
    member = await create_member(client, session_maker, instance["id"])
    template = await create_template(client)
    image = await create_image(client, template["id"])
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
        "cpu": 2,
        "ram": 8,
        "disk": 20,
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
    created = created_response.json()
    assert created["action"] == "creating"
    assert created["status"] == "pending"
    assert created["disk"] == 20

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
        json={"name": "development", "image_id": image["id"], "modules": [], "cpu": 2, "ram": 8},
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
            "cpu": ready["cpu"],
            "ram": ready["ram"],
        },
    )
    assert no_op.status_code == 200
    assert no_op.json()["updated_at"] == ready["updated_at"]

    updated = await client.put(
        f"/api/v1/workspaces/{created['id']}",
        json={
            "name": "development-updated",
            "image_id": second_image["id"],
            "modules": [],
            "cpu": 4,
            "ram": 16,
        },
    )
    assert updated.status_code == 202
    assert updated.json()["image_id"] == second_image["id"]
    assert updated.json()["disk"] == 20
    assert updated.json()["action"] == "updating"

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
            "cpu": 4,
            "ram": 16,
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
    assert deleted.json()["action"] == "deleting"


@pytest.mark.parametrize(
    "overrides",
    [
        {"cpu": 0},
        {"cpu": 9},
        {"ram": 1},
        {"ram": 33},
        {"disk": 9},
        {"disk": 101},
        {"modules": ["unknown"]},
    ],
)
async def test_workspace_creation_rejects_out_of_tolerance_configuration(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    overrides: dict[str, object],
) -> None:
    """Verify the workspace creation rejects out of tolerance configuration scenario."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    response = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(instance, member, template, image, **overrides),
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("cpu", "ram", "disk"),
    [(1, 2, 10), (8, 32, 100)],
)
async def test_workspace_creation_accepts_inclusive_boundaries(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    cpu: int,
    ram: int,
    disk: int,
) -> None:
    """Verify the workspace creation accepts inclusive boundaries scenario."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    response = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(
            instance,
            member,
            template,
            image,
            name=f"workspace-{cpu}",
            cpu=cpu,
            ram=ram,
            disk=disk,
        ),
    )
    assert response.status_code == 201


async def test_workspace_update_revalidates_cpu_ram_and_stored_disk(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the workspace update revalidates cpu ram and stored disk scenario."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    created = (
        await client.post(
            "/api/v1/workspaces", json=workspace_payload(instance, member, template, image)
        )
    ).json()
    await set_workspace_status(session_maker, created["id"])
    original = (await client.get(f"/api/v1/workspaces/{created['id']}")).json()

    for cpu, ram in ((9, 8), (2, 33)):
        rejected = await client.put(
            f"/api/v1/workspaces/{created['id']}",
            json={
                "name": "must-not-change",
                "image_id": image["id"],
                "modules": [],
                "cpu": cpu,
                "ram": ram,
            },
        )
        assert rejected.status_code == 422
        unchanged = (await client.get(f"/api/v1/workspaces/{created['id']}")).json()
        assert unchanged == original

    async with session_maker() as session:
        stored_template = await session.get(Template, UUID(str(template["id"])))
        assert stored_template is not None
        stored_template.max_disk_gb = 19
        await session.commit()

    rejected_disk = await client.put(
        f"/api/v1/workspaces/{created['id']}",
        json={
            "name": "must-not-change",
            "image_id": image["id"],
            "modules": [],
            "cpu": 2,
            "ram": 8,
        },
    )
    assert rejected_disk.status_code == 422
    assert (await client.get(f"/api/v1/workspaces/{created['id']}")).json() == original


async def test_workspace_relationship_validation_and_name_uniqueness(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the workspace relationship validation and name uniqueness scenario."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    pending_member = await create_member(
        client, session_maker, instance["id"], username="pending", ready=False
    )
    other_template = await create_template(client, name="Go")
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

    await set_workspace_status(session_maker, created.json()["id"])
    immutable = await client.put(
        f"/api/v1/workspaces/{created.json()['id']}",
        json={
            "name": "development",
            "image_id": image["id"],
            "modules": [],
            "cpu": 2,
            "ram": 8,
            "disk": 30,
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
    ).json()
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
        json={"name": "new", "image_id": image["id"], "modules": [], "cpu": 2, "ram": 8},
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
    ).json()
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
            "name": template["name"],
            "git_url": template["git_url"],
            "modules": ["git-config"],
            "version": template["version"],
            **LIMITS,
        },
    )
    assert incompatible.status_code == 409

    compatible = await client.put(
        f"/api/v1/templates/{template['id']}",
        json={
            "name": "Python updated",
            "git_url": template["git_url"],
            "modules": template["modules"],
            "version": "v2",
            **LIMITS,
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

    other_application = await create_application(client, suffix="2")
    scoped = await create_template(
        client,
        name="Scoped",
        scope="application",
        application_id=other_application["id"],
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
    ).json()
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
    ).json()
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
            "cpu": 2,
            "ram": 8,
        },
    )
    assert updated.status_code == 202
    changed_at = datetime.fromisoformat(updated.json()["updated_at"]).replace(tzinfo=UTC)
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
            application_id=None,
            scope=None,
            name="Python",
        )
        assert total == 1
        assert templates[0].id == template_id
        stored_template = await template_repository.get(template_id)
        assert stored_template is not None
        previous_updated_at = stored_template.updated_at
        unchanged_template = await template_repository.update(
            template_id,
            TemplateUpdate(
                name="Python",
                git_url="https://git.example.com/template.git",
                modules=["code-server", "git-config"],
                version="v1",
                **LIMITS,
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
                cpu=3,
                ram=12,
            ),
        )
        assert changed is True
        assert updated.disk == 20
        await repository.update_action(
            workspace_id,
            expected_action="updating",
            action="updating",
            status=WorkspaceStatus.SUCCESS,
        )
        deleted = await repository.request_deletion(workspace_id)
        assert deleted.action == "deleting"

    disposable_template = await create_template(client, name="Disposable")
    async with session_maker() as session:
        await TemplateRepository(session).delete(UUID(str(disposable_template["id"])))
