"""Coder template API behavior tests."""

import re
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coder_manager.models import JobExecution, JobStatus, Template, TemplateSyncStatus
from coder_manager.tasks import step_01_sync_template
from coder_manager.tasks.common.registry import TEMPLATE_SYNC_STEP_01_TASK

RESOURCE_LIMITS = {
    "min_cpu_count": 1,
    "max_cpu_count": 8,
    "min_ram_gb": 2,
    "max_ram_gb": 32,
    "min_disk_gb": 10,
    "max_disk_gb": 100,
}


async def create_template(
    client: AsyncClient,
    **overrides: object,
) -> dict[str, object]:
    """Create a template and return its API representation."""

    name = str(overrides.get("name", "Python"))
    payload: dict[str, object] = {
        "name": name,
        "coder_name": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
        "scope": "global",
        "application": None,
        "git_url": "https://git.example.com/templates/python.git",
        "source_path": ".",
        "branch": "main",
        "modules": ["code-server", "git-config"],
        **RESOURCE_LIMITS,
    }
    payload.update(overrides)
    response = await client.post(
        "/api/v1/templates",
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


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
            "name": "Python Updated",
            "git_url": "https://git.example.com/templates/python-v2.git",
            "source_path": "templates/python",
            "branch": "feature/new-template",
            "modules": ["jetbrains-gateway"],
            **RESOURCE_LIMITS,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Python Updated"
    assert updated.json()["scope"] == "global"
    assert updated.json()["application"] is None
    assert updated.json()["coder_name"] == "python"
    assert updated.json()["source_path"] == "templates/python"
    assert updated.json()["branch"] == "feature/new-template"
    assert updated.json()["modules"] == ["jetbrains-gateway"]
    assert updated.json()["created_at"] == created["created_at"]
    assert updated.json()["updated_at"] != created["updated_at"]

    deleted = await client.delete(f"/api/v1/templates/{created['id']}")
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert (await client.get(f"/api/v1/templates/{created['id']}")).status_code == 404


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
            "name": created["name"],
            "git_url": created["git_url"],
            "source_path": created["source_path"],
            "branch": created["branch"],
            "modules": created["modules"],
            **RESOURCE_LIMITS,
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
    """Keep the V1 contract free from local version-history endpoints."""

    paths = (await client.get("/openapi.json")).json()["paths"]
    assert all("/versions" not in path for path in paths)


async def test_identical_update_preserves_updated_at(client: AsyncClient) -> None:
    """Verify the identical update preserves updated at scenario."""

    created = await create_template(client)
    response = await client.put(
        f"/api/v1/templates/{created['id']}",
        json={
            "name": created["name"],
            "git_url": created["git_url"],
            "source_path": created["source_path"],
            "branch": created["branch"],
            "modules": created["modules"],
            **RESOURCE_LIMITS,
        },
    )

    assert response.status_code == 200
    assert response.json()["updated_at"] == created["updated_at"]


async def test_template_name_is_unique_case_insensitively_per_scope(
    client: AsyncClient,
) -> None:
    """Verify the template name is unique case insensitively per scope scenario."""

    first = "FIRST"
    second = "SECOND"
    await create_template(client, name="Python")

    duplicate_global = await client.post(
        "/api/v1/templates",
        json={
            "name": "python",
            "coder_name": "python",
            "scope": "global",
            "application": None,
            "git_url": "https://git.example.com/duplicate.git",
            "branch": "main",
            "modules": ["module"],
            **RESOURCE_LIMITS,
        },
    )
    assert duplicate_global.status_code == 409

    await create_template(
        client,
        name="Python",
        scope="application",
        application=first,
    )
    duplicate_application = await client.post(
        "/api/v1/templates",
        json={
            "name": "PYTHON",
            "coder_name": "python",
            "scope": "application",
            "application": " first ",
            "git_url": "https://git.example.com/duplicate.git",
            "branch": "main",
            "modules": ["module"],
            **RESOURCE_LIMITS,
        },
    )
    assert duplicate_application.status_code == 409

    separate_application = await create_template(
        client,
        name="python",
        scope="application",
        application=second,
    )
    assert separate_application["name"] == "python"


async def test_template_list_filters_available_templates(client: AsyncClient) -> None:
    """Verify the template list filters available templates scenario."""

    first = "FIRST"
    second = "SECOND"
    await create_template(client, name="Zulu Global")
    await create_template(
        client,
        name="Alpha First",
        scope="application",
        application=first,
    )
    await create_template(
        client,
        name="Beta Second",
        scope="application",
        application=second,
    )

    available = await client.get(
        "/api/v1/templates",
        params={"application": " first "},
    )
    assert available.status_code == 200
    assert [item["name"] for item in available.json()["items"]] == [
        "Alpha First",
        "Zulu Global",
    ]

    specific = await client.get(
        "/api/v1/templates",
        params={"application": first, "scope": "application"},
    )
    assert specific.json()["total"] == 1
    assert specific.json()["items"][0]["name"] == "Alpha First"

    named = await client.get("/api/v1/templates", params={"name": "GLOBAL"})
    assert named.json()["total"] == 1
    assert named.json()["items"][0]["name"] == "Zulu Global"

    external = await client.get(
        "/api/v1/templates",
        params={"application": "UNKNOWN"},
    )
    assert [item["name"] for item in external.json()["items"]] == ["Zulu Global"]


async def test_template_list_is_paginated_and_escapes_name_wildcards(
    client: AsyncClient,
) -> None:
    """Verify the template list is paginated and escapes name wildcards scenario."""

    percentage = await create_template(client, name="100% Template")
    await create_template(client, name="Alpha Template")

    first_page = await client.get(
        "/api/v1/templates",
        params={"page": 1, "page_size": 1},
    )
    assert first_page.json()["total"] == 2
    assert first_page.json()["pages"] == 2
    assert first_page.json()["items"][0]["name"] == "100% Template"

    literal = await client.get("/api/v1/templates", params={"name": "%"})
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
        ({"coder_name": "invalid name"}, 422),
        ({"modules": []}, 422),
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
        "name": "Python",
        "coder_name": "python",
        "scope": "global",
        "application": None,
        "git_url": "https://git.example.com/template.git",
        "branch": "main",
        "modules": ["module"],
        **RESOURCE_LIMITS,
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
            "name": "Python",
            "scope": "application",
            "application": "APP",
            "git_url": created["git_url"],
            "source_path": created["source_path"],
            "branch": created["branch"],
            "modules": created["modules"],
            **RESOURCE_LIMITS,
        },
    )
    assert immutable_scope.status_code == 422


async def test_update_name_conflict_returns_409(client: AsyncClient) -> None:
    """Verify the update name conflict returns 409 scenario."""

    await create_template(client, name="Python")
    other = await create_template(client, name="Go")
    conflict = await client.put(
        f"/api/v1/templates/{other['id']}",
        json={
            "name": "PYTHON",
            "git_url": other["git_url"],
            "source_path": other["source_path"],
            "branch": other["branch"],
            "modules": other["modules"],
            **RESOURCE_LIMITS,
        },
    )
    assert conflict.status_code == 409


async def test_missing_template_endpoints_return_404(client: AsyncClient) -> None:
    """Verify the missing template endpoints return 404 scenario."""

    template_id = uuid4()
    payload = {
        "name": "Missing",
        "git_url": "https://git.example.com/missing.git",
        "branch": "main",
        "modules": ["module"],
        **RESOURCE_LIMITS,
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
