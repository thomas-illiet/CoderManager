"""Coder template API behavior tests."""

import re
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coder_manager.models import (
    Instance,
    InstanceStatus,
    JobExecution,
    JobStatus,
    Template,
    TemplateDeployment,
    TemplateDeploymentStatus,
    TemplateSyncStatus,
)
from coder_manager.tasks import step_01_sync_template
from coder_manager.tasks.common.registry import TEMPLATE_SYNC_STEP_01_TASK


async def create_template(
    client: AsyncClient,
    **overrides: object,
) -> dict[str, object]:
    """Create a template and return its API representation."""

    display_name = str(overrides.get("display_name", "Python"))
    payload: dict[str, object] = {
        "display_name": display_name,
        "name": re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-"),
        "scope": "global",
        "application": None,
        "git_url": "https://git.example.com/templates/python.git",
        "source_path": ".",
        "branch": "main",
        "modules": ["code-server", "git-config"],
    }
    payload.update(overrides)
    response = await client.post(
        "/api/v1/templates",
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_instance(client: AsyncClient, application: str) -> dict[str, object]:
    """Create an instance and return its API resource."""

    response = await client.post(
        "/api/v1/instances",
        json={"application": application, "environment": "development"},
    )
    assert response.status_code == 201, response.text
    return response.json()["resource"]


async def test_template_crud_and_modules_contract(client: AsyncClient) -> None:
    """Verify the template crud and modules contract scenario."""

    created = await create_template(
        client,
        modules=[" code-server ", "git-config"],
        branch="main",
    )
    assert created["scope"] == "global"
    assert created["application"] is None
    assert created["modules"] == ["code-server", "git-config"]
    assert datetime.fromisoformat(str(created["created_at"]))
    assert datetime.fromisoformat(str(created["updated_at"]))

    fetched = await client.get(f"/api/v1/templates/{created['id']}")
    modules = await client.get(f"/api/v1/templates/{created['id']}/modules")
    assert fetched.status_code == 200
    assert fetched.json() == created
    assert modules.status_code == 200
    assert modules.json() == ["code-server", "git-config"]

    updated = await client.put(
        f"/api/v1/templates/{created['id']}",
        json={
            "display_name": "Python Updated",
            "git_url": "https://git.example.com/templates/python-v2.git",
            "source_path": "templates/python",
            "branch": "feature/new-template",
            "modules": ["jetbrains-gateway"],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Python Updated"
    assert updated.json()["scope"] == "global"
    assert updated.json()["application"] is None
    assert updated.json()["name"] == "python"
    assert updated.json()["source_path"] == "templates/python"
    assert updated.json()["branch"] == "feature/new-template"
    assert updated.json()["modules"] == ["jetbrains-gateway"]
    assert updated.json()["created_at"] == created["created_at"]
    assert updated.json()["updated_at"] != created["updated_at"]

    deleted = await client.delete(f"/api/v1/templates/{created['id']}")
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert (await client.get(f"/api/v1/templates/{created['id']}")).status_code == 404


async def test_template_creation_without_modules_defaults_to_empty_list(
    client: AsyncClient,
) -> None:
    """Allow templates without editable modules."""

    payload: dict[str, object] = {
        "display_name": "Managed Desktop",
        "name": "managed-desktop",
        "scope": "global",
        "application": None,
        "git_url": "https://git.example.com/templates/managed-desktop.git",
        "source_path": ".",
        "branch": "main",
    }
    response = await client.post("/api/v1/templates", json=payload)

    assert response.status_code == 201
    created = response.json()
    assert created["modules"] == []
    modules = await client.get(f"/api/v1/templates/{created['id']}/modules")
    assert modules.status_code == 200
    assert modules.json() == []


async def test_template_statistics_is_empty_without_templates(client: AsyncClient) -> None:
    """Return a direct empty array when no templates exist."""

    response = await client.get("/api/v1/templates/statistics")

    assert response.status_code == 200
    assert response.json() == []


async def test_template_statistics_classifies_compatible_ready_deployments(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Aggregate current deployment state without counting ineligible instances."""

    global_template = await create_template(client, display_name="Zulu Global")
    application_template = await create_template(
        client,
        display_name="alpha First",
        scope="application",
        application="FIRST",
    )
    no_ready_template = await create_template(
        client,
        display_name="Beta No Ready",
        scope="application",
        application="NO-READY",
    )
    instances = {
        application: await create_instance(client, application)
        for application in (
            "FIRST",
            "PENDING",
            "RUNNING",
            "ERROR",
            "MISMATCH",
            "INCOMPLETE",
            "MISSING",
            "NO-READY",
            "DELETING",
        )
    }
    instance_ids = {
        application: UUID(str(instance["id"])) for application, instance in instances.items()
    }
    global_template_id = UUID(str(global_template["id"]))
    application_template_id = UUID(str(application_template["id"]))
    target_commit = "a" * 40
    previous_commit = "b" * 40

    async with session_maker() as session:
        for application in (
            "FIRST",
            "PENDING",
            "RUNNING",
            "ERROR",
            "MISMATCH",
            "INCOMPLETE",
            "MISSING",
        ):
            instance = await session.get(Instance, instance_ids[application])
            assert instance is not None
            instance.status = InstanceStatus.SUCCESS
        deleting = await session.get(Instance, instance_ids["DELETING"])
        assert deleting is not None
        deleting.status = InstanceStatus.SUCCESS
        deleting.action = "deleting"
        session.add_all(
            [
                TemplateDeployment(
                    template_id=global_template_id,
                    instance_id=instance_ids["FIRST"],
                    target_commit=target_commit,
                    applied_commit=target_commit,
                    target_system_parameter_revision=0,
                    applied_system_parameter_revision=0,
                    status=TemplateDeploymentStatus.SUCCESS,
                ),
                TemplateDeployment(
                    template_id=global_template_id,
                    instance_id=instance_ids["PENDING"],
                    target_commit=target_commit,
                    status=TemplateDeploymentStatus.PENDING,
                ),
                TemplateDeployment(
                    template_id=global_template_id,
                    instance_id=instance_ids["RUNNING"],
                    target_commit=target_commit,
                    status=TemplateDeploymentStatus.RUNNING,
                ),
                TemplateDeployment(
                    template_id=global_template_id,
                    instance_id=instance_ids["ERROR"],
                    target_commit=target_commit,
                    applied_commit=target_commit,
                    status=TemplateDeploymentStatus.ERROR,
                ),
                TemplateDeployment(
                    template_id=global_template_id,
                    instance_id=instance_ids["MISMATCH"],
                    target_commit=target_commit,
                    applied_commit=previous_commit,
                    status=TemplateDeploymentStatus.SUCCESS,
                ),
                TemplateDeployment(
                    template_id=global_template_id,
                    instance_id=instance_ids["INCOMPLETE"],
                    status=TemplateDeploymentStatus.SUCCESS,
                ),
                TemplateDeployment(
                    template_id=global_template_id,
                    instance_id=instance_ids["NO-READY"],
                    target_commit=target_commit,
                    applied_commit=target_commit,
                    target_system_parameter_revision=0,
                    applied_system_parameter_revision=0,
                    status=TemplateDeploymentStatus.SUCCESS,
                ),
                TemplateDeployment(
                    template_id=global_template_id,
                    instance_id=instance_ids["DELETING"],
                    target_commit=target_commit,
                    applied_commit=target_commit,
                    target_system_parameter_revision=0,
                    applied_system_parameter_revision=0,
                    status=TemplateDeploymentStatus.SUCCESS,
                ),
                TemplateDeployment(
                    template_id=application_template_id,
                    instance_id=instance_ids["FIRST"],
                    target_commit=target_commit,
                    applied_commit=target_commit,
                    target_system_parameter_revision=0,
                    applied_system_parameter_revision=0,
                    status=TemplateDeploymentStatus.SUCCESS,
                ),
            ]
        )
        await session.commit()

    response = await client.get("/api/v1/templates/statistics")

    assert response.status_code == 200
    assert response.json() == [
        {
            "template_id": application_template["id"],
            "name": application_template["name"],
            "display_name": "alpha First",
            "updated": 1,
            "outdated": 0,
            "missing": 0,
        },
        {
            "template_id": no_ready_template["id"],
            "name": no_ready_template["name"],
            "display_name": "Beta No Ready",
            "updated": 0,
            "outdated": 0,
            "missing": 0,
        },
        {
            "template_id": global_template["id"],
            "name": global_template["name"],
            "display_name": "Zulu Global",
            "updated": 1,
            "outdated": 5,
            "missing": 1,
        },
    ]


async def test_template_sync_is_fire_and_forget_and_locks_mutations(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Queue one private job while keeping status and history out of the API."""

    created = await create_template(
        client,
        git_url="git@git.example.com:templates/python.git",
        branch="feature/python",
    )
    step_01_sync_template.delay.reset_mock()

    response = await client.post(f"/api/v1/templates/{created['id']}/sync")

    assert response.status_code == 202
    assert response.content == b""
    async with session_maker() as session:
        template = await session.get(Template, UUID(str(created["id"])))
        assert template is not None
        assert template.sync_status is TemplateSyncStatus.PENDING
        assert template.job_id is not None
        job = await session.get(JobExecution, template.job_id)
        assert job is not None
        assert job.task_name == TEMPLATE_SYNC_STEP_01_TASK
        assert job.status is JobStatus.PENDING
        job_id = job.id
    step_01_sync_template.delay.assert_called_once_with(str(job_id))

    second = await client.post(f"/api/v1/templates/{created['id']}/sync")
    blocked_put = await client.put(
        f"/api/v1/templates/{created['id']}",
        json={
            "display_name": created["display_name"],
            "git_url": created["git_url"],
            "source_path": created["source_path"],
            "branch": created["branch"],
            "modules": created["modules"],
        },
    )
    blocked_delete = await client.delete(f"/api/v1/templates/{created['id']}")
    assert second.status_code == 409
    assert blocked_put.status_code == 409
    assert blocked_delete.status_code == 409
    assert "sync_status" not in created
    assert "job_id" not in created

    async with session_maker() as session:
        template = await session.get(Template, UUID(str(created["id"])))
        old_job = await session.get(JobExecution, job_id)
        assert template is not None
        assert old_job is not None
        template.sync_status = TemplateSyncStatus.SUCCESS
        template.step = None
        old_job.status = JobStatus.SUCCESS
        await session.commit()

    replacement = await client.post(f"/api/v1/templates/{created['id']}/sync")
    assert replacement.status_code == 202
    async with session_maker() as session:
        assert await session.get(JobExecution, job_id) is None
        job_count = await session.scalar(
            select(func.count())
            .select_from(JobExecution)
            .where(
                JobExecution.resource_type == "template",
                JobExecution.resource_id == UUID(str(created["id"])),
            )
        )
        assert job_count == 1


async def test_no_template_version_history_api_is_exposed(client: AsyncClient) -> None:
    """Keep the V1 contract free from history routes and the former name."""

    document = (await client.get("/openapi.json")).json()
    paths = document["paths"]
    assert all("/versions" not in path for path in paths)
    assert "coder_name" not in str(document)


async def test_template_creation_openapi_shows_examples_with_and_without_modules(
    client: AsyncClient,
) -> None:
    """Document both supported template creation shapes in Swagger."""

    document = (await client.get("/openapi.json")).json()
    request_body = document["paths"]["/api/v1/templates"]["post"]["requestBody"]
    media_type = request_body["content"]["application/json"]
    examples = media_type["examples"]

    assert set(examples) == {"with_modules", "without_modules"}
    assert examples["with_modules"]["value"]["modules"] == ["code-server", "git-config"]
    assert "modules" not in examples["without_modules"]["value"]
    template_create = document["components"]["schemas"]["TemplateCreate"]
    assert "modules" not in template_create["required"]


async def test_identical_update_preserves_updated_at(client: AsyncClient) -> None:
    """Verify the identical update preserves updated at scenario."""

    created = await create_template(client)
    response = await client.put(
        f"/api/v1/templates/{created['id']}",
        json={
            "display_name": created["display_name"],
            "git_url": created["git_url"],
            "source_path": created["source_path"],
            "branch": created["branch"],
            "modules": created["modules"],
        },
    )

    assert response.status_code == 200
    assert response.json()["updated_at"] == created["updated_at"]


async def test_template_names_are_unique_case_insensitively_per_scope(
    client: AsyncClient,
) -> None:
    """Keep display and technical names unique within their effective scope."""

    first = "FIRST"
    second = "SECOND"
    await create_template(client, display_name="Python")

    duplicate_global = await client.post(
        "/api/v1/templates",
        json={
            "display_name": "python",
            "name": "python",
            "scope": "global",
            "application": None,
            "git_url": "https://git.example.com/duplicate.git",
            "branch": "main",
            "modules": ["module"],
        },
    )
    assert duplicate_global.status_code == 409

    await create_template(
        client,
        display_name="Python",
        scope="application",
        application=first,
    )
    duplicate_application = await client.post(
        "/api/v1/templates",
        json={
            "display_name": "PYTHON",
            "name": "python",
            "scope": "application",
            "application": " first ",
            "git_url": "https://git.example.com/duplicate.git",
            "branch": "main",
            "modules": ["module"],
        },
    )
    assert duplicate_application.status_code == 409

    separate_application = await create_template(
        client,
        display_name="python",
        scope="application",
        application=second,
    )
    assert separate_application["display_name"] == "python"


async def test_template_list_filters_available_templates(client: AsyncClient) -> None:
    """Verify the template list filters available templates scenario."""

    first = "FIRST"
    second = "SECOND"
    await create_template(client, display_name="Zulu Global")
    await create_template(
        client,
        display_name="Alpha First",
        scope="application",
        application=first,
    )
    await create_template(
        client,
        display_name="Beta Second",
        scope="application",
        application=second,
    )

    available = await client.get(
        "/api/v1/templates",
        params={"application": " first "},
    )
    assert available.status_code == 200
    assert [item["display_name"] for item in available.json()["items"]] == [
        "Alpha First",
        "Zulu Global",
    ]

    specific = await client.get(
        "/api/v1/templates",
        params={"application": first, "scope": "application"},
    )
    assert specific.json()["total"] == 1
    assert specific.json()["items"][0]["display_name"] == "Alpha First"

    named = await client.get("/api/v1/templates", params={"display_name": "GLOBAL"})
    assert named.json()["total"] == 1
    assert named.json()["items"][0]["display_name"] == "Zulu Global"

    external = await client.get(
        "/api/v1/templates",
        params={"application": "UNKNOWN"},
    )
    assert [item["display_name"] for item in external.json()["items"]] == ["Zulu Global"]


async def test_template_list_is_paginated_and_escapes_display_name_wildcards(
    client: AsyncClient,
) -> None:
    """Escape display-name wildcards while preserving deterministic pagination."""

    percentage = await create_template(client, display_name="100% Template")
    await create_template(client, display_name="Alpha Template")

    first_page = await client.get(
        "/api/v1/templates",
        params={"page": 1, "page_size": 1},
    )
    assert first_page.json()["total"] == 2
    assert first_page.json()["pages"] == 2
    assert first_page.json()["items"][0]["display_name"] == "100% Template"

    literal = await client.get("/api/v1/templates", params={"display_name": "%"})
    assert literal.json()["total"] == 1
    assert literal.json()["items"][0]["id"] == percentage["id"]


@pytest.mark.parametrize(
    ("overrides", "expected_status"),
    [
        ({"git_url": "http://git.example.com/template.git"}, 422),
        ({"git_url": "not-a-url"}, 422),
        ({"branch": "   "}, 422),
        ({"branch": "-unsafe"}, 422),
        ({"branch": "feature..unsafe"}, 422),
        ({"version": "legacy"}, 422),
        ({"source_path": "../outside"}, 422),
        ({"name": "invalid name"}, 422),
        ({"coder_name": "legacy"}, 422),
        ({"modules": ["module", " module "]}, 422),
        ({"modules": ["   "]}, 422),
        ({"scope": "global", "application": "APP"}, 422),
        ({"scope": "application", "application": None}, 422),
        ({"scope": "application", "application": "   "}, 422),
    ],
)
async def test_invalid_template_payloads_are_rejected(
    client: AsyncClient,
    overrides: dict[str, object],
    expected_status: int,
) -> None:
    """Verify the invalid template payloads are rejected scenario."""

    payload: dict[str, object] = {
        "display_name": "Python",
        "name": "python",
        "scope": "global",
        "application": None,
        "git_url": "https://git.example.com/template.git",
        "branch": "main",
        "modules": ["module"],
    }
    payload.update(overrides)
    response = await client.post("/api/v1/templates", json=payload)
    assert response.status_code == expected_status


async def test_external_application_is_normalized_and_update_scope_is_rejected(
    client: AsyncClient,
) -> None:
    """Normalize external identifiers while keeping template scope immutable."""

    scoped = await create_template(
        client,
        scope="application",
        application=" external-app ",
    )
    assert scoped["application"] == "EXTERNAL-APP"

    created = await create_template(client)
    immutable_scope = await client.put(
        f"/api/v1/templates/{created['id']}",
        json={
            "display_name": "Python",
            "scope": "application",
            "application": "APP",
            "git_url": created["git_url"],
            "source_path": created["source_path"],
            "branch": created["branch"],
            "modules": created["modules"],
        },
    )
    assert immutable_scope.status_code == 422

    immutable_name = await client.put(
        f"/api/v1/templates/{created['id']}",
        json={
            "display_name": "Python renamed",
            "name": "replacement-slug",
            "git_url": created["git_url"],
            "source_path": created["source_path"],
            "branch": created["branch"],
            "modules": created["modules"],
        },
    )
    assert immutable_name.status_code == 422


async def test_update_display_name_conflict_returns_409(client: AsyncClient) -> None:
    """Reject a display-name collision during replacement."""

    await create_template(client, display_name="Python")
    other = await create_template(client, display_name="Go")
    conflict = await client.put(
        f"/api/v1/templates/{other['id']}",
        json={
            "display_name": "PYTHON",
            "git_url": other["git_url"],
            "source_path": other["source_path"],
            "branch": other["branch"],
            "modules": other["modules"],
        },
    )
    assert conflict.status_code == 409


async def test_missing_template_endpoints_return_404(client: AsyncClient) -> None:
    """Verify the missing template endpoints return 404 scenario."""

    template_id = uuid4()
    payload = {
        "display_name": "Missing",
        "git_url": "https://git.example.com/missing.git",
        "branch": "main",
        "modules": ["module"],
    }
    responses = [
        await client.get(f"/api/v1/templates/{template_id}"),
        await client.get(f"/api/v1/templates/{template_id}/modules"),
        await client.put(f"/api/v1/templates/{template_id}", json=payload),
        await client.delete(f"/api/v1/templates/{template_id}"),
        await client.post(f"/api/v1/templates/{template_id}/sync"),
    ]
    assert all(response.status_code == 404 for response in responses)
    assert all(response.json() == {"detail": "Template not found"} for response in responses)
