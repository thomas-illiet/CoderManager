"""Coder template catalog API behavior tests."""

import re
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coder_manager.models import (
    Instance,
    InstanceEnvironment,
    InstanceState,
    InstanceStatus,
    JobExecution,
    JobStatus,
    Template,
    TemplateAssignment,
    TemplateAssignmentStatus,
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
        "git_url": "https://git.example.com/templates/python.git",
        "source_path": ".",
        "branch": "main",
        "modules": ["code-server", "git-config"],
    }
    payload.update(overrides)
    response = await client.post("/api/v1/templates", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def test_template_crud_and_modules_contract(client: AsyncClient) -> None:
    """Expose catalog fields without scope, application, default, or statistics state."""

    created = await create_template(
        client,
        modules=[" code-server ", "git-config"],
        branch="main",
    )

    assert set(created) == {
        "id",
        "display_name",
        "name",
        "git_url",
        "source_path",
        "branch",
        "modules",
        "system_parameter_revision",
        "created_at",
        "updated_at",
    }
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

    response = await client.post(
        "/api/v1/templates",
        json={
            "display_name": "Managed Desktop",
            "name": "managed-desktop",
            "git_url": "https://git.example.com/templates/managed-desktop.git",
            "source_path": ".",
            "branch": "main",
        },
    )

    assert response.status_code == 201
    created = response.json()
    assert created["modules"] == []
    modules = await client.get(f"/api/v1/templates/{created['id']}/modules")
    assert modules.status_code == 200
    assert modules.json() == []


async def test_template_sync_is_fire_and_forget_and_locks_mutations(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Queue one private job while keeping status and job history out of the API."""

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
        template.sync_status = TemplateSyncStatus.ERROR
        old_job.status = JobStatus.ERROR
        await session.commit()

    retryable_responses = [
        await client.post(f"/api/v1/templates/{created['id']}/sync"),
        await client.put(
            f"/api/v1/templates/{created['id']}",
            json={
                "display_name": created["display_name"],
                "git_url": created["git_url"],
                "source_path": created["source_path"],
                "branch": created["branch"],
                "modules": created["modules"],
            },
        ),
        await client.delete(f"/api/v1/templates/{created['id']}"),
    ]
    assert {item.status_code for item in retryable_responses} == {409}

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


async def test_template_openapi_omits_removed_contract(client: AsyncClient) -> None:
    """Document creation shapes without exposing scope, application, or statistics."""

    document = (await client.get("/openapi.json")).json()
    paths = document["paths"]
    request_body = paths["/api/v1/templates"]["post"]["requestBody"]
    examples = request_body["content"]["application/json"]["examples"]

    assert "/api/v1/templates/statistics" not in paths
    assert all("/versions" not in path for path in paths)
    assert set(examples) == {"with_modules", "without_modules"}
    assert examples["with_modules"]["value"]["modules"] == [
        "code-server",
        "git-config",
    ]
    assert "modules" not in examples["without_modules"]["value"]
    assert "scope" not in str(examples)
    assert "application" not in str(examples)
    for schema_name in ("TemplateCreate", "TemplateRead", "TemplateUpdate"):
        properties = document["components"]["schemas"][schema_name]["properties"]
        assert "scope" not in properties
        assert "application" not in properties
    assert all(not path.startswith("/api/v1/templates/statistics") for path in paths)
    assert "coder_name" not in str(document)
    assert "modules" not in document["components"]["schemas"]["TemplateCreate"]["required"]


async def test_identical_update_preserves_updated_at(client: AsyncClient) -> None:
    """Preserve the update timestamp when replacement changes nothing."""

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


async def test_template_names_are_global_and_display_names_may_repeat(
    client: AsyncClient,
) -> None:
    """Enforce only one case-insensitive global technical name."""

    await create_template(client, display_name="Python", name="python")
    duplicate_name = await client.post(
        "/api/v1/templates",
        json={
            "display_name": "Another Python",
            "name": "PYTHON",
            "git_url": "https://git.example.com/duplicate.git",
            "branch": "main",
            "modules": ["module"],
        },
    )
    assert duplicate_name.status_code == 409
    assert duplicate_name.json() == {
        "detail": "A template with this name already exists",
    }

    duplicate_display = await create_template(
        client,
        display_name="Python",
        name="python-two",
    )
    assert duplicate_display["display_name"] == "Python"


async def test_template_list_filters_and_paginates_by_display_name(
    client: AsyncClient,
) -> None:
    """Filter escaped display names while preserving deterministic pagination."""

    percentage = await create_template(client, display_name="100% Template")
    await create_template(client, display_name="Alpha Template")

    first_page = await client.get(
        "/api/v1/templates",
        params={"page": 1, "page_size": 1},
    )
    assert first_page.status_code == 200
    assert first_page.json()["total"] == 2
    assert first_page.json()["pages"] == 2
    assert first_page.json()["items"][0]["display_name"] == "100% Template"

    literal = await client.get("/api/v1/templates", params={"display_name": "%"})
    assert literal.status_code == 200
    assert literal.json()["total"] == 1
    assert literal.json()["items"][0]["id"] == percentage["id"]

    legacy_scope = await client.get("/api/v1/templates", params={"scope": "global"})
    legacy_application = await client.get(
        "/api/v1/templates",
        params={"application": "FIRST"},
    )
    assert legacy_scope.status_code == 422
    assert legacy_application.status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [
        {"git_url": "http://git.example.com/template.git"},
        {"git_url": "not-a-url"},
        {"branch": "   "},
        {"branch": "-unsafe"},
        {"branch": "feature..unsafe"},
        {"version": "legacy"},
        {"source_path": "../outside"},
        {"name": "invalid name"},
        {"coder_name": "legacy"},
        {"modules": ["module", " module "]},
        {"modules": ["   "]},
        {"scope": "global"},
        {"application": "APP"},
    ],
)
async def test_invalid_template_payloads_are_rejected(
    client: AsyncClient,
    overrides: dict[str, object],
) -> None:
    """Reject invalid data and every removed legacy field."""

    payload: dict[str, object] = {
        "display_name": "Python",
        "name": "python",
        "git_url": "https://git.example.com/template.git",
        "branch": "main",
        "modules": ["module"],
    }
    payload.update(overrides)
    response = await client.post("/api/v1/templates", json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("legacy_field", "legacy_value"),
    [("scope", "global"), ("application", "APP"), ("name", "replacement-slug")],
)
async def test_update_rejects_immutable_or_removed_fields(
    client: AsyncClient,
    legacy_field: str,
    legacy_value: str,
) -> None:
    """Reject technical-name replacement and removed scoping fields."""

    created = await create_template(client)
    payload = {
        "display_name": "Python renamed",
        "git_url": created["git_url"],
        "source_path": created["source_path"],
        "branch": created["branch"],
        "modules": created["modules"],
        legacy_field: legacy_value,
    }
    response = await client.put(f"/api/v1/templates/{created['id']}", json=payload)
    assert response.status_code == 422


async def test_update_allows_duplicate_display_name(client: AsyncClient) -> None:
    """Allow presentation labels to collide when technical names remain distinct."""

    await create_template(client, display_name="Python")
    other = await create_template(client, display_name="Go")
    response = await client.put(
        f"/api/v1/templates/{other['id']}",
        json={
            "display_name": "PYTHON",
            "git_url": other["git_url"],
            "source_path": other["source_path"],
            "branch": other["branch"],
            "modules": other["modules"],
        },
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "PYTHON"


async def test_catalog_mutations_conflict_with_assignments(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Serialize catalog work with active assignment transitions and preserve references."""

    created = await create_template(client)
    template_id = UUID(str(created["id"]))
    async with session_maker() as session:
        instance = Instance(
            application="TEMPLATE-LOCK",
            slug=uuid4().hex[:12],
            environment=InstanceEnvironment.DEVELOPMENT,
            action="created",
            status=InstanceStatus.SUCCESS,
            state=InstanceState.STARTED,
        )
        session.add(instance)
        await session.flush()
        session.add(
            TemplateAssignment(
                instance_id=instance.id,
                template_id=template_id,
                action="creating",
                status=TemplateAssignmentStatus.PENDING,
            )
        )
        await session.commit()

    payload = {
        "display_name": "Python updated",
        "git_url": created["git_url"],
        "source_path": created["source_path"],
        "branch": created["branch"],
        "modules": created["modules"],
    }
    update = await client.put(f"/api/v1/templates/{template_id}", json=payload)
    sync = await client.post(f"/api/v1/templates/{template_id}/sync")
    delete = await client.delete(f"/api/v1/templates/{template_id}")

    assert update.status_code == 409
    assert update.json() == {
        "detail": "Template assignment operation is already in progress",
    }
    assert sync.status_code == 409
    assert sync.json() == update.json()
    assert delete.status_code == 409
    assert delete.json() == {"detail": "Template is still assigned to instances"}

    async with session_maker() as session:
        assignment = await session.scalar(
            select(TemplateAssignment).where(TemplateAssignment.template_id == template_id)
        )
        assert assignment is not None
        assignment.status = TemplateAssignmentStatus.ERROR
        await session.commit()

    error_update = await client.put(f"/api/v1/templates/{template_id}", json=payload)
    error_sync = await client.post(f"/api/v1/templates/{template_id}/sync")
    assert error_update.status_code == 409
    assert error_update.json() == update.json()
    assert error_sync.status_code == 409
    assert error_sync.json() == update.json()


async def test_missing_template_endpoints_return_404(client: AsyncClient) -> None:
    """Return one stable not-found response from every catalog item endpoint."""

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
