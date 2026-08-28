"""Explicit template assignment synchronization and deletion worker tests."""

# ruff: noqa: C901, PLR0915, SLF001

from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Self
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from coder_manager import tasks, worker_database
from coder_manager.config import Settings
from coder_manager.crypto import InstancePasswordCipher
from coder_manager.domains.coder import CoderTemplate, CoderTemplateVersion
from coder_manager.domains.template_source import TemplateArchive
from coder_manager.models import (
    Instance,
    InstanceState,
    JobExecution,
    JobStatus,
    Template,
    TemplateAssignment,
    TemplateAssignmentStatus,
    TemplateDeployment,
    TemplateDeploymentStatus,
    TemplateSyncStatus,
    Workspace,
    WorkspaceStatus,
)
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    claim_execution,
    prepare_execution_retry,
)
from coder_manager.tasks.common.registry import (
    TEMPLATE_ASSIGNMENT_CREATE_STEP_01,
    TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
    TEMPLATE_SYNC_STEP_01_TASK,
)
from coder_manager.tasks.template._sync import (
    TemplateSourceSnapshot,
    TemplateTargetClaimLostError,
    TemplateTargetSyncError,
    sync_template_target,
)
from coder_manager.utils.instance_urls import InstancePublicUrlConfig
from tests.conftest import TEST_CRYPTO_KEY
from tests.test_workspaces import (
    create_instance,
    create_ready_context,
    create_template,
    set_instance_status,
    workspace_payload,
)


def configure_worker(
    monkeypatch: pytest.MonkeyPatch,
    sync_session_maker: sessionmaker[Session],
) -> None:
    """Route worker persistence to the isolated test database."""

    monkeypatch.setattr(
        worker_database,
        "get_worker_session_maker",
        lambda: sync_session_maker,
    )


async def store_admin_password(
    session_maker: async_sessionmaker[AsyncSession],
    instance_id: UUID,
) -> None:
    """Store one decryptable administrator password for worker-side Coder calls."""

    async with session_maker() as session:
        instance = await session.get(Instance, instance_id)
        assert instance is not None
        instance.password_enc = InstancePasswordCipher(SecretStr(TEST_CRYPTO_KEY)).encrypt(
            SecretStr("password"), instance_id
        )
        await session.commit()


async def put_assignment(
    client: AsyncClient,
    instance_id: object,
    template_id: object,
) -> tuple[UUID, UUID]:
    """Create one explicit assignment and return its assignment and job IDs."""

    response = await client.put(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert response.status_code == 202, response.text
    payload = response.json()
    return UUID(payload["resource"]["id"]), UUID(payload["job"]["id"])


async def mark_assignment_stable(
    session_maker: async_sessionmaker[AsyncSession],
    assignment_id: UUID,
) -> None:
    """Move an assignment and its original job to their converged state."""

    async with session_maker() as session:
        assignment = await session.get(TemplateAssignment, assignment_id)
        assert assignment is not None
        assignment.action = "created"
        assignment.status = TemplateAssignmentStatus.SUCCESS
        assignment.step = None
        if assignment.job_id is not None:
            job = await session.get(JobExecution, assignment.job_id)
            assert job is not None
            job.status = JobStatus.SUCCESS
            job.claimed_at = None
        await session.commit()


async def queued_template_job(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    template_id: object,
) -> UUID:
    """Queue a template-wide synchronization and return its durable job ID."""

    response = await client.post(f"/api/v1/templates/{template_id}/sync")
    assert response.status_code == 202, response.text
    async with session_maker() as session:
        template = await session.get(Template, UUID(str(template_id)))
        assert template is not None
        assert template.job_id is not None
        return template.job_id


def test_template_sync_helpers_reject_missing_local_resources(
    sync_session_maker: sessionmaker[Session],
) -> None:
    """Fail safely when a source, assignment, deployment, or target disappeared."""

    sync_helpers = import_module("coder_manager.tasks.template._sync")
    missing_template = uuid4()
    missing_assignment = uuid4()
    missing_claim = ExecutionClaim(
        job_id=uuid4(),
        task_name=TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
        step=TEMPLATE_ASSIGNMENT_CREATE_STEP_01,
        attempt=1,
        resource_type="template_assignment",
        resource_id=missing_assignment,
    )
    url_config = InstancePublicUrlConfig.from_settings(
        Settings(instance_base_domain="emea.code-studio.echonet")
    )
    with pytest.raises(TemplateTargetSyncError, match="Template is missing"):
        sync_helpers.template_source_snapshot(missing_template, sync_session_maker)
    with pytest.raises(TemplateTargetSyncError, match="assignment is missing"):
        sync_helpers.assignment_template_id(missing_assignment, sync_session_maker)
    with pytest.raises(TemplateTargetSyncError, match="Template is missing"):
        sync_helpers.stable_assignment_ids(missing_template, sync_session_maker)
    with pytest.raises(TemplateTargetClaimLostError, match="claim is no longer current"):
        sync_helpers._prepare_deployment(
            missing_claim,
            missing_assignment,
            missing_template,
            "a" * 40,
            0,
            sync_session_maker,
            url_config,
        )
    with pytest.raises(TemplateTargetClaimLostError, match="claim is no longer current"):
        sync_helpers._store_remote_ids(
            missing_claim,
            missing_assignment,
            organization_id=uuid4(),
            session_factory=sync_session_maker,
        )
    with pytest.raises(TemplateTargetClaimLostError, match="claim is no longer current"):
        sync_helpers._finish_deployment(
            missing_claim,
            missing_assignment,
            "a" * 40,
            0,
            success=True,
            session_factory=sync_session_maker,
        )
    delete_helpers = import_module("coder_manager.tasks.template.delete.step_01_delete_template")
    with pytest.raises(RuntimeError, match="deletion claim is no longer current"):
        delete_helpers._heartbeat_owned(missing_claim, sync_session_maker)


async def test_template_sync_rejects_assignment_without_admin_password(
    client: AsyncClient,
    sync_session_maker: sessionmaker[Session],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Stop before Coder calls when the assigned instance has no administrator."""

    instance = await create_instance(client, "NO PASSWORD")
    instance_id = UUID(str(instance["id"]))
    await set_instance_status(session_maker, instance_id, state=InstanceState.STARTED)
    template = await create_template(client)
    assignment_id, job_id = await put_assignment(client, instance_id, template["id"])
    claim = claim_execution(
        job_id,
        TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
        sync_session_maker,
    )
    assert claim is not None
    snapshot = TemplateSourceSnapshot(
        id=UUID(str(template["id"])),
        display_name="Python",
        name="python",
        git_url="https://git.example.com/template.git",
        source_path=".",
        branch="main",
        system_parameter_revision=0,
    )
    with pytest.raises(TemplateTargetSyncError, match="password is not initialized"):
        sync_template_target(
            snapshot,
            TemplateArchive(commit="a" * 40, content=b"ustar"),
            assignment_id,
            sync_session_maker,
            claim=claim,
        )


async def test_template_sync_without_assignments_completes_without_git_fetch(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complete an empty explicit target set without contacting the Git source."""

    configure_worker(monkeypatch, sync_session_maker)
    template = await create_template(client, display_name="No Assignments")
    job_id = await queued_template_job(client, session_maker, template["id"])
    sync_module = import_module("coder_manager.tasks.template.sync.step_01_sync_template")
    monkeypatch.setattr(
        sync_module,
        "fetch_template_archive",
        lambda _snapshot: pytest.fail("Git must not be fetched without assignments"),
    )

    assert tasks.step_01_sync_template.run(str(job_id)) == {"status": "success"}
    async with session_maker() as session:
        stored = await session.get(Template, UUID(str(template["id"])))
        job = await session.get(JobExecution, job_id)
        assert stored is not None
        assert job is not None
        assert stored.sync_status is TemplateSyncStatus.SUCCESS
        assert job.status is JobStatus.SUCCESS


async def test_template_sync_targets_only_stable_explicit_assignments_and_retries(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ignore unassigned and in-flight targets, then retry a partial durable failure."""

    configure_worker(monkeypatch, sync_session_maker)
    first = await create_instance(client, "FIRST")
    second = await create_instance(client, "SECOND")
    third = await create_instance(client, "THIRD")
    for instance in (first, second, third):
        await set_instance_status(
            session_maker,
            instance["id"],
            state=InstanceState.STARTED,
        )
    template = await create_template(client, display_name="Explicit Python")
    stable_id, _stable_job_id = await put_assignment(client, first["id"], template["id"])
    unavailable_id, _unavailable_job_id = await put_assignment(client, second["id"], template["id"])
    await mark_assignment_stable(session_maker, stable_id)
    await mark_assignment_stable(session_maker, unavailable_id)
    async with session_maker() as session:
        unavailable_instance = await session.get(Instance, UUID(str(second["id"])))
        assert unavailable_instance is not None
        unavailable_instance.state = InstanceState.STOPPED
        await session.commit()

    job_id = await queued_template_job(client, session_maker, template["id"])
    sync_module = import_module("coder_manager.tasks.template.sync.step_01_sync_template")
    monkeypatch.setattr(
        sync_module,
        "fetch_template_archive",
        lambda _snapshot: TemplateArchive(commit="a" * 40, content=b"ustar"),
    )
    targeted: list[UUID] = []

    def fail_target(
        _snapshot: TemplateSourceSnapshot,
        _archive: TemplateArchive,
        assignment_id: UUID,
        _session_factory: sessionmaker[Session],
        *,
        claim: object,
        heartbeat: object,
    ) -> bool:
        """Fail the sole stable assignment after recording its identity."""

        del claim, heartbeat
        targeted.append(assignment_id)
        message = "remote unavailable"
        raise RuntimeError(message)

    monkeypatch.setattr(sync_module, "sync_template_target", fail_target)
    with pytest.raises(RuntimeError, match="1 target"):
        tasks.step_01_sync_template.run(str(job_id))

    assert targeted == [stable_id]
    assert unavailable_id not in targeted
    async with session_maker() as session:
        stored_template = await session.get(Template, UUID(str(template["id"])))
        job = await session.get(JobExecution, job_id)
        assert stored_template is not None
        assert job is not None
        assert stored_template.sync_status is TemplateSyncStatus.ERROR
        assert job.status is JobStatus.ERROR

    monkeypatch.setattr(sync_module, "sync_template_target", lambda *_args, **_kwargs: True)
    assert tasks.step_01_sync_template.run(str(job_id)) == {"status": "success"}
    async with session_maker() as session:
        stored_template = await session.get(Template, UUID(str(template["id"])))
        job = await session.get(JobExecution, job_id)
        assert stored_template is not None
        assert job is not None
        assert stored_template.sync_status is TemplateSyncStatus.SUCCESS
        assert job.attempt == 2


async def test_assignment_create_worker_is_durable_and_marks_created(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry an assignment publication and expose it only after convergence."""

    configure_worker(monkeypatch, sync_session_maker)
    instance = await create_instance(client, "ASSIGN RETRY")
    await set_instance_status(
        session_maker,
        instance["id"],
        state=InstanceState.STARTED,
    )
    template = await create_template(client, display_name="Assigned Python")
    assignment_id, job_id = await put_assignment(client, instance["id"], template["id"])
    create_module = import_module("coder_manager.tasks.template.create.step_01_create_template")
    monkeypatch.setattr(
        create_module,
        "fetch_template_archive",
        lambda _snapshot: TemplateArchive(commit="a" * 40, content=b"ustar"),
    )
    attempts = 0

    def converge(*_args: object, **_kwargs: object) -> bool:
        """Fail once, then converge the same durable assignment."""

        nonlocal attempts
        attempts += 1
        if attempts == 1:
            message = "temporary Coder failure"
            raise RuntimeError(message)
        return True

    monkeypatch.setattr(create_module, "sync_template_target", converge)
    with pytest.raises(RuntimeError, match="temporary Coder failure"):
        tasks.step_01_create_template.run(str(job_id))
    async with session_maker() as session:
        failed = await session.get(TemplateAssignment, assignment_id)
        job = await session.get(JobExecution, job_id)
        assert failed is not None
        assert job is not None
        assert failed.action == "creating"
        assert failed.status is TemplateAssignmentStatus.ERROR
        assert job.status is JobStatus.ERROR

    assert tasks.step_01_create_template.run(str(job_id)) == {"status": "success"}
    async with session_maker() as session:
        created = await session.get(TemplateAssignment, assignment_id)
        job = await session.get(JobExecution, job_id)
        assert created is not None
        assert job is not None
        assert created.action == "created"
        assert created.status is TemplateAssignmentStatus.SUCCESS
        assert created.step is None
        assert job.status is JobStatus.SUCCESS
        assert job.attempt == 2


async def test_applied_assignment_commit_skips_all_remote_calls(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use assignment deployment state to avoid republishing an unchanged commit."""

    instance = await create_instance(client, "UNCHANGED")
    instance_id = UUID(str(instance["id"]))
    await set_instance_status(session_maker, instance_id, state=InstanceState.STARTED)
    template = await create_template(client)
    template_id = UUID(str(template["id"]))
    assignment_id, job_id = await put_assignment(client, instance_id, template_id)
    claim = claim_execution(
        job_id,
        TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
        sync_session_maker,
    )
    assert claim is not None
    await store_admin_password(session_maker, instance_id)
    commit = "b" * 40
    async with session_maker() as session:
        session.add(
            TemplateDeployment(
                assignment_id=assignment_id,
                target_commit=commit,
                applied_commit=commit,
                target_system_parameter_revision=0,
                applied_system_parameter_revision=0,
                status=TemplateDeploymentStatus.SUCCESS,
            )
        )
        await session.commit()

    sync_helpers = import_module("coder_manager.tasks.template._sync")
    monkeypatch.setattr(
        sync_helpers,
        "CoderClient",
        lambda *_args, **_kwargs: pytest.fail("Coder must not be contacted"),
    )
    snapshot = TemplateSourceSnapshot(
        id=template_id,
        display_name="Python",
        name="python",
        git_url="git@git.example.com:templates/python.git",
        source_path=".",
        branch="main",
        system_parameter_revision=0,
    )
    assert (
        sync_template_target(
            snapshot,
            TemplateArchive(commit=commit, content=b"ustar"),
            assignment_id,
            sync_session_maker,
            claim=claim,
        )
        is False
    )


async def test_reclaimed_claim_cannot_overwrite_deployment_state(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fence every deployment write after Beat reclaims and reruns the same job."""

    instance = await create_instance(client, "STALE DEPLOYMENT")
    instance_id = UUID(str(instance["id"]))
    await set_instance_status(session_maker, instance_id, state=InstanceState.STARTED)
    template = await create_template(client, display_name="Stale Deployment")
    template_id = UUID(str(template["id"]))
    assignment_id, job_id = await put_assignment(client, instance_id, template_id)
    await store_admin_password(session_maker, instance_id)
    sync_helpers = import_module("coder_manager.tasks.template._sync")
    monkeypatch.setattr(
        sync_helpers,
        "get_settings",
        lambda: Settings(crypto_key=TEST_CRYPTO_KEY),
    )
    stale_claim = claim_execution(
        job_id,
        TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
        sync_session_maker,
    )
    assert stale_claim is not None
    sync_helpers._prepare_deployment(
        stale_claim,
        assignment_id,
        template_id,
        "e" * 40,
        0,
        sync_session_maker,
        InstancePublicUrlConfig.from_settings(Settings()),
    )

    assert (
        prepare_execution_retry(
            job_id,
            stale_before=datetime.now(UTC) + timedelta(seconds=1),
            session_factory=sync_session_maker,
        )
        == TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK
    )
    current_claim = claim_execution(
        job_id,
        TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
        sync_session_maker,
    )
    assert current_claim is not None
    assert current_claim.attempt == stale_claim.attempt + 1

    with pytest.raises(TemplateTargetClaimLostError, match="claim is no longer current"):
        sync_helpers._store_remote_ids(
            stale_claim,
            assignment_id,
            organization_id=uuid4(),
            coder_template_id=uuid4(),
            session_factory=sync_session_maker,
        )
    with pytest.raises(TemplateTargetClaimLostError, match="claim is no longer current"):
        sync_helpers._finish_deployment(
            stale_claim,
            assignment_id,
            "e" * 40,
            0,
            success=True,
            session_factory=sync_session_maker,
        )

    organization_id = uuid4()
    coder_template_id = uuid4()
    sync_helpers._store_remote_ids(
        current_claim,
        assignment_id,
        organization_id=organization_id,
        coder_template_id=coder_template_id,
        session_factory=sync_session_maker,
    )
    sync_helpers._finish_deployment(
        current_claim,
        assignment_id,
        "e" * 40,
        0,
        success=True,
        session_factory=sync_session_maker,
    )
    async with session_maker() as session:
        deployment = await session.scalar(
            select(TemplateDeployment).where(TemplateDeployment.assignment_id == assignment_id)
        )
        assert deployment is not None
        assert deployment.coder_organization_id == organization_id
        assert deployment.coder_template_id == coder_template_id
        assert deployment.status is TemplateDeploymentStatus.SUCCESS
        assert deployment.applied_commit == "e" * 40


async def test_template_sync_claim_owns_explicit_assignment_deployment_writes(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow a template-wide job to fence writes through its stable assignment."""

    instance = await create_instance(client, "TEMPLATE CLAIM")
    instance_id = UUID(str(instance["id"]))
    await set_instance_status(session_maker, instance_id, state=InstanceState.STARTED)
    template = await create_template(client, display_name="Template Claim")
    template_id = UUID(str(template["id"]))
    assignment_id, _assignment_job_id = await put_assignment(
        client,
        instance_id,
        template_id,
    )
    await mark_assignment_stable(session_maker, assignment_id)
    await store_admin_password(session_maker, instance_id)
    sync_job_id = await queued_template_job(client, session_maker, template_id)
    claim = claim_execution(sync_job_id, TEMPLATE_SYNC_STEP_01_TASK, sync_session_maker)
    assert claim is not None
    sync_helpers = import_module("coder_manager.tasks.template._sync")
    monkeypatch.setattr(
        sync_helpers,
        "get_settings",
        lambda: Settings(crypto_key=TEST_CRYPTO_KEY),
    )
    sync_helpers._prepare_deployment(
        claim,
        assignment_id,
        template_id,
        "7" * 40,
        0,
        sync_session_maker,
        InstancePublicUrlConfig.from_settings(Settings()),
    )
    sync_helpers._finish_deployment(
        claim,
        assignment_id,
        "7" * 40,
        0,
        success=True,
        session_factory=sync_session_maker,
    )
    async with session_maker() as session:
        deployment = await session.scalar(
            select(TemplateDeployment).where(TemplateDeployment.assignment_id == assignment_id)
        )
        assert deployment is not None
        assert deployment.status is TemplateDeploymentStatus.SUCCESS
        assert deployment.applied_commit == "7" * 40


async def test_reassigned_resource_fences_original_claim_deployment_writes(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a still-running old job after its assignment is owned by another job."""

    instance = await create_instance(client, "REASSIGNED DEPLOYMENT")
    instance_id = UUID(str(instance["id"]))
    await set_instance_status(session_maker, instance_id, state=InstanceState.STARTED)
    template = await create_template(client, display_name="Reassigned Deployment")
    template_id = UUID(str(template["id"]))
    assignment_id, job_id = await put_assignment(client, instance_id, template_id)
    await store_admin_password(session_maker, instance_id)
    sync_helpers = import_module("coder_manager.tasks.template._sync")
    monkeypatch.setattr(
        sync_helpers,
        "get_settings",
        lambda: Settings(crypto_key=TEST_CRYPTO_KEY),
    )
    original_claim = claim_execution(
        job_id,
        TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
        sync_session_maker,
    )
    assert original_claim is not None
    sync_helpers._prepare_deployment(
        original_claim,
        assignment_id,
        template_id,
        "f" * 40,
        0,
        sync_session_maker,
        InstancePublicUrlConfig.from_settings(Settings()),
    )

    replacement_job_id = uuid4()
    with sync_session_maker() as session:
        session.add(
            JobExecution(
                id=replacement_job_id,
                name="template_assignment.create",
                task_name=TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
                resource_type="template_assignment",
                resource_id=assignment_id,
                step=TEMPLATE_ASSIGNMENT_CREATE_STEP_01,
                status=JobStatus.PENDING,
            )
        )
        assignment = session.get(TemplateAssignment, assignment_id)
        assert assignment is not None
        assignment.job_id = replacement_job_id
        assignment.status = TemplateAssignmentStatus.PENDING
        session.commit()

    with pytest.raises(TemplateTargetClaimLostError, match="claim is no longer current"):
        sync_helpers._store_remote_ids(
            original_claim,
            assignment_id,
            coder_template_id=uuid4(),
            session_factory=sync_session_maker,
        )
    async with session_maker() as session:
        deployment = await session.scalar(
            select(TemplateDeployment).where(TemplateDeployment.assignment_id == assignment_id)
        )
        assert deployment is not None
        assert deployment.coder_template_id is None
        assert deployment.status is TemplateDeploymentStatus.RUNNING


def test_remote_template_ownership_rejects_foreign_adoption_and_allows_recovery() -> None:
    """Require a persisted template or version identity before adopting a same-name row."""

    sync_helpers = import_module("coder_manager.tasks.template._sync")
    organization_id = uuid4()
    template_id = uuid4()
    version_id = uuid4()

    class FakeClient:
        """Expose configurable ID and name lookups to the ownership fence."""

        persisted: CoderTemplate | None = None
        named: CoderTemplate | None = None

        def template(self, _template_id: UUID) -> CoderTemplate | None:
            """Return the configured persisted-ID lookup result."""

            return self.persisted

        def template_by_name(self, _organization_id: UUID, _name: str) -> CoderTemplate | None:
            """Return the configured organization-and-name lookup result."""

            return self.named

    foreign = FakeClient()
    foreign.named = CoderTemplate(template_id, uuid4())
    with pytest.raises(TemplateTargetSyncError, match="owned outside CoderManager"):
        sync_helpers._managed_remote_template(foreign, organization_id, "python", None, None)

    by_id = FakeClient()
    by_id.persisted = CoderTemplate(template_id, version_id)
    by_id.named = CoderTemplate(template_id, version_id)
    assert sync_helpers._managed_remote_template(
        by_id, organization_id, "python", template_id, None
    ) == (by_id.persisted, True)

    conflicting_name = FakeClient()
    conflicting_name.persisted = CoderTemplate(template_id, version_id)
    conflicting_name.named = CoderTemplate(uuid4(), uuid4())
    with pytest.raises(TemplateTargetSyncError, match="owned outside CoderManager"):
        sync_helpers._managed_remote_template(
            conflicting_name,
            organization_id,
            "python",
            template_id,
            version_id,
        )

    by_version = FakeClient()
    by_version.named = CoderTemplate(template_id, version_id)
    assert sync_helpers._managed_remote_template(
        by_version, organization_id, "python", None, version_id
    ) == (by_version.named, True)


async def test_missing_persisted_version_is_recreated_during_recovery(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recreate a retry version that was deleted remotely outside CoderManager."""

    instance = await create_instance(client, "MISSING REMOTE VERSION")
    instance_id = UUID(str(instance["id"]))
    await set_instance_status(session_maker, instance_id, state=InstanceState.STARTED)
    template = await create_template(client, display_name="Recovered Python")
    template_id = UUID(str(template["id"]))
    assignment_id, job_id = await put_assignment(client, instance_id, template_id)
    await store_admin_password(session_maker, instance_id)
    claim = claim_execution(
        job_id,
        TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
        sync_session_maker,
    )
    assert claim is not None
    organization_id = uuid4()
    remote_template_id = uuid4()
    missing_version_id = uuid4()
    recreated_version_id = uuid4()
    commit = "9" * 40
    async with session_maker() as session:
        session.add(
            TemplateDeployment(
                assignment_id=assignment_id,
                coder_organization_id=organization_id,
                coder_template_id=remote_template_id,
                coder_template_version_id=missing_version_id,
                target_commit=commit,
                target_system_parameter_revision=0,
                status=TemplateDeploymentStatus.ERROR,
            )
        )
        await session.commit()

    calls: list[str] = []

    class FakeCoderClient:
        """Expose a managed template whose persisted retry version disappeared."""

        def __init__(self, instance_url: str) -> None:
            """Validate the assigned Coder endpoint."""

            assert instance_url == instance["instance_url"]

        def __enter__(self) -> Self:
            """Return the fake context-managed client."""

            return self

        def __exit__(self, *_args: object) -> None:
            """Leave the fake client context."""

        def authenticate_prepared_admin(self, password: SecretStr) -> None:
            """Validate the decrypted administrator password."""

            assert password.get_secret_value() == "password"

        def default_organization_id(self) -> UUID:
            """Return the persisted organization identity."""

            return organization_id

        def template(self, selected_template_id: UUID) -> CoderTemplate:
            """Recover the managed remote template by persisted ID."""

            assert selected_template_id == remote_template_id
            return CoderTemplate(remote_template_id, missing_version_id)

        def template_by_name(
            self,
            selected_organization_id: UUID,
            name: str,
        ) -> CoderTemplate:
            """Return the same managed template by its deterministic name."""

            assert (selected_organization_id, name) == (organization_id, "recovered-python")
            return CoderTemplate(remote_template_id, missing_version_id)

        def template_version_by_name(
            self,
            selected_organization_id: UUID,
            template_name: str,
            version_name: str,
        ) -> None:
            """Report that the deterministic version name is also absent."""

            assert selected_organization_id == organization_id
            assert template_name == "recovered-python"
            assert version_name == f"git-{commit}-p0"

        def template_version_for_recovery(
            self,
            version_id: UUID,
        ) -> None:
            """Treat the externally deleted persisted version as absent."""

            assert version_id == missing_version_id
            calls.append("missing-version")

        def upload_template_archive(self, content: bytes) -> UUID:
            """Upload the source again after recovery lookup returns absence."""

            assert content == b"ustar"
            calls.append("upload")
            return uuid4()

        def create_template_version(
            self,
            selected_organization_id: UUID,
            *,
            file_id: UUID,
            version_name: str,
            template_id: UUID | None,
            user_variable_values: tuple[tuple[str, str], ...] = (),
        ) -> CoderTemplateVersion:
            """Create a replacement version on the recovered template."""

            assert selected_organization_id == organization_id
            assert isinstance(file_id, UUID)
            assert version_name == f"git-{commit}-p0"
            assert template_id == remote_template_id
            assert user_variable_values == ()
            calls.append("create-version")
            return CoderTemplateVersion(recreated_version_id, "succeeded", archived=False)

        def activate_template_version(self, template_id: UUID, version_id: UUID) -> None:
            """Activate the recreated version on the recovered template."""

            assert (template_id, version_id) == (remote_template_id, recreated_version_id)
            calls.append("activate")

    sync_helpers = import_module("coder_manager.tasks.template._sync")
    monkeypatch.setattr(sync_helpers, "CoderClient", FakeCoderClient)
    monkeypatch.setattr(
        sync_helpers,
        "get_settings",
        lambda: Settings(crypto_key=TEST_CRYPTO_KEY),
    )
    changed = sync_template_target(
        TemplateSourceSnapshot(
            id=template_id,
            display_name="Recovered Python",
            name="recovered-python",
            git_url="https://git.example.com/template.git",
            source_path=".",
            branch="main",
            system_parameter_revision=0,
        ),
        TemplateArchive(commit=commit, content=b"ustar"),
        assignment_id,
        sync_session_maker,
        claim=claim,
    )

    assert changed is True
    assert calls == ["missing-version", "upload", "create-version", "activate"]
    async with session_maker() as session:
        deployment = await session.scalar(
            select(TemplateDeployment).where(TemplateDeployment.assignment_id == assignment_id)
        )
        assert deployment is not None
        assert deployment.coder_template_version_id == recreated_version_id
        assert deployment.status is TemplateDeploymentStatus.SUCCESS
        assert deployment.applied_commit == commit


async def test_target_sync_creates_first_remote_template(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist remote identities around a first assignment upload and import."""

    instance = await create_instance(client, "FIRST REMOTE")
    instance_id = UUID(str(instance["id"]))
    await set_instance_status(session_maker, instance_id, state=InstanceState.STARTED)
    template = await create_template(client)
    template_id = UUID(str(template["id"]))
    parameter = await client.post(
        f"/api/v1/templates/{template_id}/parameters",
        json={
            "type": "system",
            "name": "registry_url",
            "display_name": "Registry URL",
            "value": "registry.example.com",
        },
    )
    assert parameter.status_code == 201
    assignment_id, job_id = await put_assignment(client, instance_id, template_id)
    claim = claim_execution(
        job_id,
        TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
        sync_session_maker,
    )
    assert claim is not None
    await store_admin_password(session_maker, instance_id)

    organization_id = UUID("10000000-0000-0000-0000-000000000001")
    expected_version_id = UUID("20000000-0000-0000-0000-000000000002")
    remote_template_id = UUID("30000000-0000-0000-0000-000000000003")
    calls: list[str] = []

    class FakeCoderClient:
        """Record the first-publication Coder operations."""

        def __init__(self, instance_url: str) -> None:
            """Validate the selected assignment endpoint."""

            assert instance_url == instance["instance_url"]

        def __enter__(self) -> Self:
            """Return the fake context-managed client."""

            return self

        def __exit__(self, *_args: object) -> None:
            """Leave the fake client context."""

        def authenticate_prepared_admin(self, password: SecretStr) -> None:
            """Validate the decrypted administrator password."""

            assert password.get_secret_value() == "password"
            calls.append("authenticate")

        def default_organization_id(self) -> UUID:
            """Return the sole default organization identity."""

            return organization_id

        def template_by_name(self, selected_organization: UUID, name: str) -> CoderTemplate | None:
            """Report that the assignment has no same-name remote template."""

            assert (selected_organization, name) == (organization_id, "python")
            return None

        def upload_template_archive(self, content: bytes) -> UUID:
            """Validate and identify the uploaded source archive."""

            assert content == b"ustar"
            calls.append("upload")
            return UUID("40000000-0000-0000-0000-000000000004")

        def create_template_version(
            self,
            selected_organization: UUID,
            *,
            file_id: UUID,
            version_name: str,
            template_id: UUID | None,
            user_variable_values: tuple[tuple[str, str], ...] = (),
        ) -> CoderTemplateVersion:
            """Create the deterministic pending remote version."""

            assert selected_organization == organization_id
            assert file_id == UUID("40000000-0000-0000-0000-000000000004")
            assert version_name == f"git-{'d' * 40}-p1"
            assert template_id is None
            assert user_variable_values == (("registry_url", "registry.example.com"),)
            calls.append("create-version")
            return CoderTemplateVersion(expected_version_id, "pending", archived=False)

        def wait_template_version(
            self,
            selected_version: UUID,
            *,
            timeout_seconds: float,
            poll_interval_seconds: float,
            heartbeat: object,
        ) -> CoderTemplateVersion:
            """Complete the deterministic import while exercising the heartbeat."""

            assert selected_version == expected_version_id
            assert timeout_seconds > poll_interval_seconds
            assert callable(heartbeat)
            heartbeat()
            calls.append("wait")
            return CoderTemplateVersion(expected_version_id, "succeeded", archived=False)

        def create_template(
            self,
            selected_organization: UUID,
            *,
            name: str,
            display_name: str,
            version_id: UUID,
        ) -> CoderTemplate:
            """Create the first remote template from the imported version."""

            assert (selected_organization, name, display_name, version_id) == (
                organization_id,
                "python",
                "Python",
                expected_version_id,
            )
            calls.append("create-template")
            return CoderTemplate(remote_template_id, expected_version_id)

    sync_helpers = import_module("coder_manager.tasks.template._sync")
    monkeypatch.setattr(sync_helpers, "CoderClient", FakeCoderClient)
    monkeypatch.setattr(
        sync_helpers,
        "get_settings",
        lambda: Settings(crypto_key=TEST_CRYPTO_KEY),
    )
    heartbeat_calls: list[bool] = []
    changed = sync_template_target(
        TemplateSourceSnapshot(
            id=template_id,
            display_name="Python",
            name="python",
            git_url="git@git.example.com:templates/python.git",
            source_path=".",
            branch="main",
            system_parameter_revision=1,
        ),
        TemplateArchive(commit="d" * 40, content=b"ustar"),
        assignment_id,
        sync_session_maker,
        claim=claim,
        heartbeat=lambda: heartbeat_calls.append(True),
    )

    assert changed is True
    assert heartbeat_calls == [True]
    assert calls == ["authenticate", "upload", "create-version", "wait", "create-template"]
    async with session_maker() as session:
        deployment = await session.scalar(
            select(TemplateDeployment).where(TemplateDeployment.assignment_id == assignment_id)
        )
        assert deployment is not None
        assert deployment.coder_organization_id == organization_id
        assert deployment.coder_template_id == remote_template_id
        assert deployment.coder_template_version_id == expected_version_id
        assert deployment.target_commit == "d" * 40
        assert deployment.applied_commit == "d" * 40
        assert deployment.status is TemplateDeploymentStatus.SUCCESS


async def test_assignment_delete_retries_then_cascades_remote_and_local_state(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete template workspaces first and retain local state until remote convergence."""

    configure_worker(monkeypatch, sync_session_maker)
    instance, member, template, image = await create_ready_context(client, session_maker)
    instance_id = UUID(str(instance["id"]))
    template_id = UUID(str(template["id"]))
    await set_instance_status(session_maker, instance_id, state=InstanceState.STARTED)
    await store_admin_password(session_maker, instance_id)
    workspace_response = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(instance, member, template, image),
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace_id = UUID(workspace_response.json()["resource"]["id"])
    async with session_maker() as session:
        workspace = await session.get(Workspace, workspace_id)
        assignment = await session.scalar(
            select(TemplateAssignment).where(
                TemplateAssignment.instance_id == instance_id,
                TemplateAssignment.template_id == template_id,
            )
        )
        assert workspace is not None
        assert assignment is not None
        workspace.status = WorkspaceStatus.SUCCESS
        assignment_id = assignment.id
        deployment = await session.scalar(
            select(TemplateDeployment).where(TemplateDeployment.assignment_id == assignment_id)
        )
        assert deployment is not None
        remote_template_id = deployment.coder_template_id
        assert remote_template_id is not None
        await session.commit()

    deletion = await client.delete(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert deletion.status_code == 202, deletion.text
    job_id = UUID(deletion.json()["job"]["id"])
    delete_module = import_module("coder_manager.tasks.template.delete.step_01_delete_template")
    monkeypatch.setattr(
        delete_module,
        "get_settings",
        lambda: Settings(crypto_key=TEST_CRYPTO_KEY),
    )
    events: list[str] = []
    attempts = 0

    def delete_remote_workspaces(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        """Fail the first remote cleanup without allowing local deletion."""

        nonlocal attempts
        attempts += 1
        events.append("workspaces")
        if attempts == 1:
            message = "Coder temporarily unavailable"
            raise RuntimeError(message)
        return (str(uuid4()),)

    class FakeCoderClient:
        """Record authentication and idempotent remote template deletion."""

        def __init__(self, instance_url: str) -> None:
            """Validate the selected assignment endpoint."""

            assert instance_url == instance["instance_url"]

        def __enter__(self) -> Self:
            """Return the fake context-managed client."""

            return self

        def __exit__(self, *_args: object) -> None:
            """Leave the fake client context."""

        def authenticate_prepared_admin(self, password: SecretStr) -> None:
            """Validate the decrypted administrator password."""

            assert password.get_secret_value() == "password"
            events.append("authenticate")

        def delete_template(self, selected_template_id: UUID) -> None:
            """Record deletion of the exact persisted remote template."""

            assert selected_template_id == remote_template_id
            events.append("template")

    monkeypatch.setattr(
        delete_module.coder,
        "delete_template_workspaces",
        delete_remote_workspaces,
    )
    monkeypatch.setattr(delete_module.coder, "CoderClient", FakeCoderClient)
    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        tasks.step_01_delete_template.run(str(job_id))
    async with session_maker() as session:
        assert await session.get(TemplateAssignment, assignment_id) is not None
        assert await session.get(Workspace, workspace_id) is not None
        assert (
            await session.scalar(
                select(TemplateDeployment).where(TemplateDeployment.assignment_id == assignment_id)
            )
            is not None
        )
        job = await session.get(JobExecution, job_id)
        assert job is not None
        assert job.status is JobStatus.ERROR

    assert tasks.step_01_delete_template.run(str(job_id)) == {"status": "deleted"}
    assert events == ["workspaces", "workspaces", "authenticate", "template"]
    async with session_maker() as session:
        assert await session.get(TemplateAssignment, assignment_id) is None
        assert await session.get(Workspace, workspace_id) is None
        assert (
            await session.scalar(
                select(TemplateDeployment).where(TemplateDeployment.assignment_id == assignment_id)
            )
            is None
        )
        assert await session.get(Template, template_id) is not None
        job = await session.get(JobExecution, job_id)
        assert job is not None
        assert job.status is JobStatus.SUCCESS
        assert job.attempt == 2

    repeated = await client.delete(f"/api/v1/instances/{instance_id}/templates/{template_id}")
    assert repeated.status_code == 204
