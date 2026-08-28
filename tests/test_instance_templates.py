"""Explicit instance template assignment API contract tests."""

# ruff: noqa: PLR0915

import re
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, Response
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coder_manager.api.routes import instance_templates as instance_template_routes
from coder_manager.models import (
    Instance,
    InstanceEnvironment,
    InstanceState,
    InstanceStatus,
    JobExecution,
    JobStatus,
    Member,
    MemberRole,
    MemberStatus,
    Template,
    TemplateAssignment,
    TemplateAssignmentStatus,
    TemplateImage,
    TemplateSyncStatus,
    Workspace,
    WorkspaceStatus,
)
from coder_manager.repositories import (
    TemplateAssignmentActionConflictError,
    TemplateAssignmentInstanceNotFoundError,
    TemplateAssignmentInstanceUnavailableError,
    TemplateAssignmentJobConflictError,
    TemplateAssignmentRepository,
    TemplateAssignmentSyncInProgressError,
    TemplateAssignmentTemplateNotFoundError,
    TemplateAssignmentWorkspacesBusyError,
)
from coder_manager.tasks import step_01_create_template, step_01_delete_template
from coder_manager.tasks.common.registry import (
    TEMPLATE_ASSIGNMENT_CREATE_STEP_01,
    TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
    TEMPLATE_ASSIGNMENT_DELETE_STEP_01,
    TEMPLATE_ASSIGNMENT_DELETE_STEP_01_TASK,
)


async def create_template(
    client: AsyncClient,
    *,
    display_name: str = "Python",
    name: str | None = None,
) -> dict[str, object]:
    """Create one catalog template through its public API."""

    response = await client.post(
        "/api/v1/templates",
        json={
            "display_name": display_name,
            "name": name or re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-"),
            "git_url": "https://git.example.com/templates/python.git",
            "source_path": ".",
            "branch": "main",
            "modules": ["code-server"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_instance(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    ready: bool = True,
    application: str | None = None,
) -> UUID:
    """Persist one instance in a ready or unavailable state."""

    async with session_maker() as session:
        instance = Instance(
            application=application or f"ASSIGN-{uuid4().hex[:8].upper()}",
            slug=uuid4().hex[:12],
            environment=InstanceEnvironment.DEVELOPMENT,
            action="created" if ready else "creating",
            status=InstanceStatus.SUCCESS if ready else InstanceStatus.PENDING,
            state=InstanceState.STARTED if ready else InstanceState.STOPPED,
        )
        session.add(instance)
        await session.commit()
        return instance.id


async def converge_assignment(
    session_maker: async_sessionmaker[AsyncSession],
    instance_id: UUID,
    template_id: UUID,
) -> tuple[UUID, UUID]:
    """Mark an assignment and its creation job as successfully converged."""

    async with session_maker() as session:
        assignment = await session.scalar(
            select(TemplateAssignment).where(
                TemplateAssignment.instance_id == instance_id,
                TemplateAssignment.template_id == template_id,
            )
        )
        assert assignment is not None
        assert assignment.job_id is not None
        job = await session.get(JobExecution, assignment.job_id)
        assert job is not None
        assignment.action = "created"
        assignment.status = TemplateAssignmentStatus.SUCCESS
        assignment.step = None
        job.status = JobStatus.SUCCESS
        await session.commit()
        return assignment.id, job.id


async def test_repository_transitions_are_idempotent_and_keep_one_job_per_action(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Exercise the durable state machine directly across committed sessions."""

    template = await create_template(client, display_name="Repository transitions")
    template_id = UUID(str(template["id"]))
    instance_id = await create_instance(session_maker)

    async with session_maker() as session:
        repository = TemplateAssignmentRepository(session)
        created, accepted = await repository.put(instance_id, template_id)
        assert accepted is True
        assert created.job_id is not None
        assignment_id = created.id
        creation_job_id = created.job_id

    async with session_maker() as session:
        repository = TemplateAssignmentRepository(session)
        repeated, accepted = await repository.put(instance_id, template_id)
        assert accepted is True
        assert repeated.id == assignment_id
        assert repeated.job_id == creation_job_id

        assignment = await session.get(TemplateAssignment, assignment_id)
        job = await session.get(JobExecution, creation_job_id)
        assert assignment is not None
        assert job is not None
        assignment.action = "created"
        assignment.status = TemplateAssignmentStatus.SUCCESS
        assignment.step = None
        job.status = JobStatus.SUCCESS
        await session.commit()

    async with session_maker() as session:
        repository = TemplateAssignmentRepository(session)
        converged, accepted = await repository.put(instance_id, template_id)
        assert accepted is False
        assert converged.id == assignment_id

        listed, total = await repository.list(instance_id, page=1, page_size=20)
        assert total == 1
        assert [item.id for item in listed] == [assignment_id]

        deleting, accepted = await repository.request_deletion(instance_id, template_id)
        assert accepted is True
        assert deleting is not None
        assert deleting.action == "deleting"
        assert deleting.job_id is not None
        deletion_job_id = deleting.job_id

    async with session_maker() as session:
        repeated, accepted = await TemplateAssignmentRepository(session).request_deletion(
            instance_id,
            template_id,
        )
        assert accepted is True
        assert repeated is not None
        assert repeated.id == assignment_id
        assert repeated.job_id == deletion_job_id


async def test_put_is_idempotent_and_returns_no_job_once_converged(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Create once, reuse retryable work, then return a synchronous current state."""

    template = await create_template(client)
    template_id = UUID(str(template["id"]))
    instance_id = await create_instance(session_maker)
    step_01_create_template.delay.reset_mock()

    first = await client.put(f"/api/v1/instances/{instance_id}/templates/{template_id}")

    assert first.status_code == 202
    first_body = first.json()
    resource = first_body["resource"]
    job = first_body["job"]
    assert resource["instance_id"] == str(instance_id)
    assert resource["template"] == template
    assert resource["action"] == "creating"
    assert resource["status"] == "pending"
    assert resource["step"] == TEMPLATE_ASSIGNMENT_CREATE_STEP_01
    assert resource["deployment_status"] is None
    assert resource["target_commit"] is None
    assert resource["applied_commit"] is None
    assert resource["target_system_parameter_revision"] is None
    assert resource["applied_system_parameter_revision"] is None
    assert job["name"] == "template_assignment.create"
    assert job["resource_type"] == "template_assignment"
    assert job["resource_id"] == resource["id"]
    assert job["step"] == TEMPLATE_ASSIGNMENT_CREATE_STEP_01
    step_01_create_template.delay.assert_called_once_with(job["id"])

    repeated = await client.put(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert repeated.status_code == 202
    assert repeated.json()["resource"]["id"] == resource["id"]
    assert repeated.json()["job"]["id"] == job["id"]
    step_01_create_template.delay.assert_called_once()

    async with session_maker() as session:
        assignment = await session.get(TemplateAssignment, UUID(resource["id"]))
        durable_job = await session.get(JobExecution, UUID(job["id"]))
        assert assignment is not None
        assert durable_job is not None
        assignment.status = TemplateAssignmentStatus.ERROR
        durable_job.status = JobStatus.ERROR
        await session.commit()

    retryable = await client.put(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert retryable.status_code == 202
    assert retryable.json()["job"]["id"] == job["id"]
    assert retryable.json()["resource"]["status"] == "error"
    step_01_create_template.delay.assert_called_once()

    assignment_id, creation_job_id = await converge_assignment(
        session_maker,
        instance_id,
        template_id,
    )
    converged = await client.put(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert converged.status_code == 200
    assert converged.json()["resource"]["id"] == str(assignment_id)
    assert converged.json()["resource"]["action"] == "created"
    assert converged.json()["resource"]["status"] == "success"
    assert converged.json()["resource"]["job_id"] == str(creation_job_id)
    assert converged.json()["job"] is None

    async with session_maker() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(JobExecution)
            .where(JobExecution.name == "template_assignment.create")
        )
        assert count == 1
        stored_job = await session.get(JobExecution, creation_job_id)
        assert stored_job is not None
        assert stored_job.task_name == TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK


async def test_delete_is_idempotent_and_absence_is_an_empty_204(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Reuse deletion work and skip mutation preconditions once already absent."""

    template = await create_template(client)
    template_id = UUID(str(template["id"]))
    instance_id = await create_instance(session_maker)
    created = await client.put(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert created.status_code == 202
    assignment_id, creation_job_id = await converge_assignment(
        session_maker,
        instance_id,
        template_id,
    )
    step_01_delete_template.delay.reset_mock()

    first = await client.delete(f"/api/v1/instances/{instance_id}/templates/{template_id}")

    assert first.status_code == 202
    first_body = first.json()
    assert first_body["resource"]["id"] == str(assignment_id)
    assert first_body["resource"]["action"] == "deleting"
    assert first_body["resource"]["status"] == "pending"
    assert first_body["resource"]["step"] == TEMPLATE_ASSIGNMENT_DELETE_STEP_01
    assert first_body["job"]["name"] == "template_assignment.delete"
    assert first_body["job"]["resource_type"] == "template_assignment"
    assert first_body["job"]["resource_id"] == str(assignment_id)
    assert first_body["job"]["step"] == TEMPLATE_ASSIGNMENT_DELETE_STEP_01
    deletion_job_id = UUID(first_body["job"]["id"])
    assert deletion_job_id != creation_job_id
    step_01_delete_template.delay.assert_called_once_with(str(deletion_job_id))

    repeated = await client.delete(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert repeated.status_code == 202
    assert repeated.json()["job"]["id"] == str(deletion_job_id)
    step_01_delete_template.delay.assert_called_once()

    async with session_maker() as session:
        assignment = await session.get(TemplateAssignment, assignment_id)
        deletion_job = await session.get(JobExecution, deletion_job_id)
        assert assignment is not None
        assert deletion_job is not None
        assignment.status = TemplateAssignmentStatus.ERROR
        deletion_job.status = JobStatus.ERROR
        await session.commit()

    retryable = await client.delete(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert retryable.status_code == 202
    assert retryable.json()["job"]["id"] == str(deletion_job_id)
    assert retryable.json()["resource"]["status"] == "error"
    step_01_delete_template.delay.assert_called_once()

    async with session_maker() as session:
        assignment = await session.get(TemplateAssignment, assignment_id)
        instance = await session.get(Instance, instance_id)
        assert assignment is not None
        assert instance is not None
        await session.delete(assignment)
        instance.state = InstanceState.STOPPED
        instance.status = InstanceStatus.ERROR
        await session.commit()

    absent = await client.delete(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert absent.status_code == 204
    assert absent.content == b""
    assert step_01_delete_template.delay.call_count == 1
    assert (await client.get(f"/api/v1/templates/{template_id}")).status_code == 200

    async with session_maker() as session:
        deletion_job = await session.get(JobExecution, deletion_job_id)
        assert deletion_job is not None
        assert deletion_job.task_name == TEMPLATE_ASSIGNMENT_DELETE_STEP_01_TASK


async def test_get_is_paginated_and_readable_while_instance_is_stopped(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """List assignments deterministically without mutation readiness checks."""

    zulu = await create_template(client, display_name="Zulu", name="zulu")
    alpha = await create_template(client, display_name="alpha", name="alpha")
    instance_id = await create_instance(session_maker)
    for template in (zulu, alpha):
        response = await client.put(f"/api/v1/instances/{instance_id}/templates/{template['id']}")
        assert response.status_code == 202

    async with session_maker() as session:
        instance = await session.get(Instance, instance_id)
        assert instance is not None
        instance.state = InstanceState.STOPPED
        instance.status = InstanceStatus.ERROR
        await session.commit()

    first = await client.get(
        f"/api/v1/instances/{instance_id}/templates",
        params={"page": 1, "page_size": 1},
    )
    second = await client.get(
        f"/api/v1/instances/{instance_id}/templates",
        params={"page": 2, "page_size": 1},
    )

    assert first.status_code == 200
    assert first.json()["total"] == 2
    assert first.json()["pages"] == 2
    assert first.json()["page"] == 1
    assert first.json()["page_size"] == 1
    assert first.json()["items"][0]["template"]["id"] == alpha["id"]
    assert second.status_code == 200
    assert second.json()["items"][0]["template"]["id"] == zulu["id"]
    invalid = await client.get(
        f"/api/v1/instances/{instance_id}/templates",
        params={"page": 0},
    )
    assert invalid.status_code == 422


async def test_assignment_routes_return_precise_not_found_and_conflict_responses(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Validate parents first and reject unavailable, syncing, or inconsistent work."""

    template = await create_template(client)
    template_id = UUID(str(template["id"]))
    ready_id = await create_instance(session_maker)
    unavailable_id = await create_instance(session_maker, ready=False)
    missing_id = uuid4()

    missing_instance_responses = [
        await client.get(f"/api/v1/instances/{missing_id}/templates"),
        await client.put(f"/api/v1/instances/{missing_id}/templates/{template_id}"),
        await client.delete(f"/api/v1/instances/{missing_id}/templates/{template_id}"),
    ]
    assert all(response.status_code == 404 for response in missing_instance_responses)
    assert all(
        response.json() == {"detail": "Instance not found"}
        for response in missing_instance_responses
    )

    missing_template_responses = [
        await client.put(f"/api/v1/instances/{ready_id}/templates/{missing_id}"),
        await client.delete(f"/api/v1/instances/{ready_id}/templates/{missing_id}"),
    ]
    assert all(response.status_code == 404 for response in missing_template_responses)
    assert all(
        response.json() == {"detail": "Template not found"}
        for response in missing_template_responses
    )

    unavailable = await client.put(f"/api/v1/instances/{unavailable_id}/templates/{template_id}")
    assert unavailable.status_code == 409
    assert unavailable.json() == {"detail": "Instance must be started and ready"}

    requested_sync = await client.post(f"/api/v1/templates/{template_id}/sync")
    assert requested_sync.status_code == 202
    syncing = await client.put(f"/api/v1/instances/{ready_id}/templates/{template_id}")
    assert syncing.status_code == 409
    assert syncing.json() == {
        "detail": "Template synchronization is already in progress",
    }

    async with session_maker() as session:
        stored_template = await session.get(Template, template_id)
        assert stored_template is not None
        assert stored_template.job_id is not None
        sync_job = await session.get(JobExecution, stored_template.job_id)
        assert sync_job is not None
        stored_template.sync_status = TemplateSyncStatus.ERROR
        sync_job.status = JobStatus.ERROR
        await session.commit()
    retryable_sync = await client.put(f"/api/v1/instances/{ready_id}/templates/{template_id}")
    assert retryable_sync.status_code == 409
    assert retryable_sync.json() == syncing.json()

    async with session_maker() as session:
        stored_template = await session.get(Template, template_id)
        assert stored_template is not None
        assert stored_template.job_id is not None
        sync_job = await session.get(JobExecution, stored_template.job_id)
        assert sync_job is not None
        stored_template.sync_status = TemplateSyncStatus.SUCCESS
        sync_job.status = JobStatus.SUCCESS
        await session.commit()
    creating = await client.put(f"/api/v1/instances/{ready_id}/templates/{template_id}")
    assert creating.status_code == 202
    opposite_action = await client.delete(f"/api/v1/instances/{ready_id}/templates/{template_id}")
    assert opposite_action.status_code == 409
    assert opposite_action.json() == {
        "detail": "Template assignment has a conflicting action",
    }

    async with session_maker() as session:
        assignment = await session.scalar(
            select(TemplateAssignment).where(
                TemplateAssignment.instance_id == ready_id,
                TemplateAssignment.template_id == template_id,
            )
        )
        assert assignment is not None
        assignment.job_id = None
        await session.commit()
    inconsistent = await client.put(f"/api/v1/instances/{ready_id}/templates/{template_id}")
    assert inconsistent.status_code == 409
    assert inconsistent.json() == {
        "detail": "Template assignment durable job is inconsistent",
    }


async def test_retryable_assignment_errors_block_instance_and_configuration_lifecycles(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Keep every incompatible lifecycle fenced while Beat owns an error retry."""

    template = await create_template(client)
    template_id = UUID(str(template["id"]))
    instance_id = await create_instance(session_maker)
    response = await client.put(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert response.status_code == 202
    assignment_id = UUID(response.json()["resource"]["id"])
    job_id = UUID(response.json()["job"]["id"])

    async with session_maker() as session:
        assignment = await session.get(TemplateAssignment, assignment_id)
        job = await session.get(JobExecution, job_id)
        assert assignment is not None
        assert job is not None
        assignment.status = TemplateAssignmentStatus.ERROR
        job.status = JobStatus.ERROR
        await session.commit()

    instance_mutations = [
        await client.post(f"/api/v1/instances/{instance_id}/start"),
        await client.post(f"/api/v1/instances/{instance_id}/stop"),
        await client.post(f"/api/v1/instances/{instance_id}/sync"),
        await client.delete(f"/api/v1/instances/{instance_id}"),
    ]
    member = await client.post(
        f"/api/v1/instances/{instance_id}/members",
        json={"username": "alice", "role": "user"},
    )
    parameter = await client.post(
        f"/api/v1/templates/{template_id}/parameters",
        json={
            "type": "user",
            "name": "project_name",
            "display_name": "Project name",
            "description": "Workspace project",
            "required": False,
            "mutable": True,
            "default_value": None,
        },
    )

    assert all(item.status_code == 409 for item in instance_mutations)
    assert member.status_code == 409
    assert parameter.status_code == 409


async def test_retryable_template_sync_blocks_assigned_instance_lifecycles(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Keep instance and configuration work fenced until template sync converges."""

    template = await create_template(client)
    template_id = UUID(str(template["id"]))
    instance_id = await create_instance(session_maker)
    response = await client.put(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert response.status_code == 202
    await converge_assignment(session_maker, instance_id, template_id)

    async with session_maker() as session:
        stored_template = await session.get(Template, template_id)
        assert stored_template is not None
        stored_template.sync_status = TemplateSyncStatus.ERROR
        await session.commit()

    instance_mutations = [
        await client.post(f"/api/v1/instances/{instance_id}/start"),
        await client.post(f"/api/v1/instances/{instance_id}/stop"),
        await client.post(f"/api/v1/instances/{instance_id}/sync"),
        await client.delete(f"/api/v1/instances/{instance_id}"),
    ]
    member = await client.post(
        f"/api/v1/instances/{instance_id}/members",
        json={"username": "alice", "role": "user"},
    )
    parameter = await client.post(
        f"/api/v1/templates/{template_id}/parameters",
        json={
            "type": "user",
            "name": "project_name",
            "display_name": "Project name",
            "description": "Workspace project",
            "required": False,
            "mutable": True,
            "default_value": None,
        },
    )

    assert all(item.status_code == 409 for item in instance_mutations)
    assert member.status_code == 409
    assert parameter.status_code == 409


async def test_existing_assignment_requires_a_ready_instance_to_delete(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Apply readiness checks to an existing assignment, unlike an absent one."""

    template = await create_template(client)
    template_id = UUID(str(template["id"]))
    instance_id = await create_instance(session_maker)
    response = await client.put(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert response.status_code == 202
    await converge_assignment(session_maker, instance_id, template_id)
    async with session_maker() as session:
        instance = await session.get(Instance, instance_id)
        assert instance is not None
        instance.state = InstanceState.STOPPED
        instance.status = InstanceStatus.SUCCESS
        await session.commit()

    blocked = await client.delete(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert blocked.status_code == 409
    assert blocked.json() == {"detail": "Instance must be started and ready"}


@pytest.mark.parametrize(
    "busy_status",
    [WorkspaceStatus.PENDING, WorkspaceStatus.RUNNING, WorkspaceStatus.ERROR],
)
async def test_delete_blocks_retryable_local_workspaces(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    busy_status: WorkspaceStatus,
) -> None:
    """Block retryable work and let the deletion worker clean successful rows."""

    template = await create_template(client)
    template_id = UUID(str(template["id"]))
    instance_id = await create_instance(session_maker)
    response = await client.put(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert response.status_code == 202
    await converge_assignment(session_maker, instance_id, template_id)

    async with session_maker() as session:
        member = Member(
            instance_id=instance_id,
            username=f"user-{uuid4().hex[:8]}",
            role=MemberRole.USER,
            action="created",
            status=MemberStatus.SUCCESS,
        )
        image = TemplateImage(
            template_id=template_id,
            registry_name="registry.example.com",
            name="python",
            version=uuid4().hex[:8],
        )
        session.add_all([member, image])
        await session.flush()
        workspace = Workspace(
            name=f"ws-{uuid4().hex[:8]}",
            instance_id=instance_id,
            template_id=template_id,
            member_id=member.id,
            image_id=image.id,
            modules=[],
            parameters={},
            action="creating",
            status=busy_status,
        )
        session.add(workspace)
        await session.commit()
        workspace_id = workspace.id

    busy = await client.delete(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert busy.status_code == 409
    assert busy.json() == {
        "detail": "Template assignment has workspaces with an action in progress",
    }

    async with session_maker() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.action = "created"
        workspace.status = WorkspaceStatus.SUCCESS
        await session.commit()

    accepted = await client.delete(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert accepted.status_code == 202
    assert accepted.json()["resource"]["action"] == "deleting"


async def test_assignment_openapi_contract(client: AsyncClient) -> None:
    """Expose pagination and dynamic mutation statuses without phantom request bodies."""

    document = (await client.get("/openapi.json")).json()
    paths = document["paths"]
    collection = paths["/api/v1/instances/{instance_id}/templates"]
    item = paths["/api/v1/instances/{instance_id}/templates/{template_id}"]

    assert set(collection) == {"get"}
    assert set(item) == {"put", "delete"}
    assert "requestBody" not in item["put"]
    assert {"200", "202"}.issubset(item["put"]["responses"])
    assert {"202", "204"}.issubset(item["delete"]["responses"])
    assert "content" not in item["delete"]["responses"]["204"]
    assert (
        collection["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/InstanceTemplatePage"
    )
    assignment_fields = document["components"]["schemas"]["InstanceTemplateRead"]["properties"]
    assert {
        "id",
        "instance_id",
        "template",
        "action",
        "status",
        "job_id",
        "step",
        "deployment_status",
        "target_commit",
        "applied_commit",
        "target_system_parameter_revision",
        "applied_system_parameter_revision",
        "created_at",
        "updated_at",
    } == set(assignment_fields)


@pytest.mark.parametrize(
    ("repository_error", "expected_status", "expected_detail"),
    [
        (TemplateAssignmentInstanceNotFoundError, 404, "Instance not found"),
        (TemplateAssignmentTemplateNotFoundError, 404, "Template not found"),
        (TemplateAssignmentInstanceUnavailableError, 409, "Instance must be started and ready"),
        (
            TemplateAssignmentSyncInProgressError,
            409,
            "Template synchronization is already in progress",
        ),
        (
            TemplateAssignmentActionConflictError,
            409,
            "Template assignment has a conflicting action",
        ),
        (
            TemplateAssignmentJobConflictError,
            409,
            "Template assignment durable job is inconsistent",
        ),
    ],
)
async def test_put_assignment_route_error_mappings(
    monkeypatch: pytest.MonkeyPatch,
    repository_error: type[Exception],
    expected_status: int,
    expected_detail: str,
) -> None:
    """Map every repository PUT failure without requiring database setup."""

    class FailingRepository:
        """Raise the selected transition failure."""

        def __init__(self, _session: object) -> None:
            """Accept the route dependency placeholder."""

        async def put(self, _instance_id: UUID, _template_id: UUID) -> None:
            """Fail the assignment transition."""

            raise repository_error

    monkeypatch.setattr(
        instance_template_routes,
        "TemplateAssignmentRepository",
        FailingRepository,
    )
    with pytest.raises(HTTPException) as caught:
        await instance_template_routes.put_instance_template(
            uuid4(),
            uuid4(),
            Response(),
            None,  # type: ignore[arg-type]
        )
    assert caught.value.status_code == expected_status
    assert caught.value.detail == expected_detail


@pytest.mark.parametrize(
    ("repository_error", "expected_status", "expected_detail"),
    [
        (TemplateAssignmentInstanceNotFoundError, 404, "Instance not found"),
        (TemplateAssignmentTemplateNotFoundError, 404, "Template not found"),
        (TemplateAssignmentInstanceUnavailableError, 409, "Instance must be started and ready"),
        (
            TemplateAssignmentSyncInProgressError,
            409,
            "Template synchronization is already in progress",
        ),
        (
            TemplateAssignmentActionConflictError,
            409,
            "Template assignment has a conflicting action",
        ),
        (
            TemplateAssignmentJobConflictError,
            409,
            "Template assignment durable job is inconsistent",
        ),
        (
            TemplateAssignmentWorkspacesBusyError,
            409,
            "Template assignment has workspaces with an action in progress",
        ),
    ],
)
async def test_delete_assignment_route_error_mappings(
    monkeypatch: pytest.MonkeyPatch,
    repository_error: type[Exception],
    expected_status: int,
    expected_detail: str,
) -> None:
    """Map every repository DELETE failure without requiring database setup."""

    class FailingRepository:
        """Raise the selected transition failure."""

        def __init__(self, _session: object) -> None:
            """Accept the route dependency placeholder."""

        async def request_deletion(self, _instance_id: UUID, _template_id: UUID) -> None:
            """Fail the assignment transition."""

            raise repository_error

    monkeypatch.setattr(
        instance_template_routes,
        "TemplateAssignmentRepository",
        FailingRepository,
    )
    with pytest.raises(HTTPException) as caught:
        await instance_template_routes.delete_instance_template(
            uuid4(),
            uuid4(),
            None,  # type: ignore[arg-type]
        )
    assert caught.value.status_code == expected_status
    assert caught.value.detail == expected_detail


async def test_list_assignment_route_not_found_and_empty_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover list parent validation and helper branches without durable state."""

    class FailingRepository:
        """Reject an unknown list parent."""

        def __init__(self, _session: object) -> None:
            """Accept the route dependency placeholder."""

        async def list(self, *_args: object, **_kwargs: object) -> None:
            """Fail parent validation."""

            raise TemplateAssignmentInstanceNotFoundError

    monkeypatch.setattr(
        instance_template_routes,
        "TemplateAssignmentRepository",
        FailingRepository,
    )
    with pytest.raises(HTTPException) as caught:
        await instance_template_routes.list_instance_templates(
            uuid4(),
            None,  # type: ignore[arg-type]
        )
    assert caught.value.status_code == 404
    assert (
        await instance_template_routes._job_read(None, None) is None  # noqa: SLF001
    )  # type: ignore[arg-type]

    class EmptySession:
        """Expose an empty dispatch staging dictionary."""

        def __init__(self) -> None:
            """Initialize one empty dispatch staging dictionary."""

            self.info: dict[str, object] = {}

    instance_template_routes._dispatch_enqueued_job(  # noqa: SLF001
        EmptySession(),
        TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
    )  # type: ignore[arg-type]
