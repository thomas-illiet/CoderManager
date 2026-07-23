"""Durable Celery step and recovery tests."""

# ruff: noqa: EM101, PLR0913, PLR0915, S105, SLF001, TRY003

from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from coder_manager import tasks, worker_database
from coder_manager.celery_app import celery_app
from coder_manager.config import Settings, get_settings
from coder_manager.crypto import PasswordCipher
from coder_manager.domains import argocd, postgresql
from coder_manager.models import (
    Database,
    DatabaseAllocation,
    Instance,
    InstanceKubernetes,
    InstanceStatus,
    JobExecution,
    JobStatus,
    Member,
    MemberStatus,
    Workspace,
    WorkspaceStatus,
)
from coder_manager.tasks.common.execution import (
    claim_execution,
    complete_execution,
    prepare_execution_retry,
)
from coder_manager.tasks.common.registry import (
    INSTANCE_CREATE_STEP_01_TASK,
    INSTANCE_CREATE_STEP_02,
    INSTANCE_CREATE_STEP_02_TASK,
    INSTANCE_DELETE_STEP_04,
    INSTANCE_DELETE_STEP_04_TASK,
    INSTANCE_UPDATE_STEP_01,
    INSTANCE_UPDATE_STEP_01_TASK,
    REGISTERED_STEP_NAMES,
    dispatch_registered_step,
)
from coder_manager.tasks.instance import _database as database_helpers
from tests.conftest import TEST_CRYPTO_KEY
from tests.test_workspaces import (
    create_instance,
    create_ready_context,
    set_instance_status,
    workspace_payload,
)


def configure_worker(
    monkeypatch: pytest.MonkeyPatch,
    sync_session_maker: sessionmaker[Session],
) -> None:
    """Route worker persistence and crypto configuration to test fixtures."""

    monkeypatch.setattr(
        worker_database,
        "get_worker_session_maker",
        lambda: sync_session_maker,
    )
    monkeypatch.setattr(
        database_helpers,
        "get_settings",
        lambda: Settings(crypto_key=TEST_CRYPTO_KEY),
    )


async def encrypt_allocated_database(
    session_maker: async_sessionmaker[AsyncSession],
    instance_id: UUID,
) -> None:
    """Replace the fixture password with a valid encrypted envelope."""

    async with session_maker() as session:
        row = (
            await session.execute(
                select(DatabaseAllocation, Database)
                .join(Database, Database.id == DatabaseAllocation.database_id)
                .where(DatabaseAllocation.instance_id == instance_id)
            )
        ).one()
        _allocation, database = row
        database.password_enc = PasswordCipher(SecretStr(TEST_CRYPTO_KEY)).encrypt(
            SecretStr("managed-secret"),
            database.id,
        )
        await session.commit()


def successful_reconcile(
    instance_id: UUID,
    attached_name: str | None,
    _members: tuple[tuple[str, str], ...],
    _region: str,
    _environment: str,
) -> str:
    """Return a deterministic Argo CD Application name."""

    return attached_name or f"coder-{instance_id.hex}"


def test_registered_step_names_and_beat_schedule() -> None:
    """Register only explicit steps and the generic recovery control task."""

    assert {
        task.name
        for task in (
            tasks.step_01_create_schema,
            tasks.step_02_create_instance,
            tasks.step_01_update_instance,
            tasks.step_01_remove_workspaces,
            tasks.step_02_remove_instance,
            tasks.step_03_remove_schema,
            tasks.step_04_remove_local_configuration,
            tasks.step_01_create_workspace,
            tasks.step_01_update_workspace,
            tasks.step_01_delete_workspace,
            tasks.step_01_sync_database,
        )
    } == REGISTERED_STEP_NAMES
    assert not hasattr(tasks, "upsert_instance")
    schedule = celery_app.conf.beat_schedule["retry-job-executions"]
    assert schedule["task"] == "coder_manager.retry_job_executions"
    assert schedule["schedule"] == timedelta(seconds=get_settings().job_retry_interval_seconds)
    task_source = Path(tasks.__file__).parent
    assert all("chain(" not in path.read_text() for path in task_source.rglob("*.py"))


async def test_create_steps_advance_after_commit_and_finish_instance(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create the schema before directly scheduling remote instance creation."""

    configure_worker(monkeypatch, sync_session_maker)
    instance = await create_instance(client, "STEP CREATE")
    instance_id = UUID(str(instance["id"]))
    job_id = UUID(str(instance["job_id"]))
    await encrypt_allocated_database(session_maker, instance_id)
    created_targets: list[postgresql.SchemaTarget] = []
    monkeypatch.setattr(postgresql, "create_schema", created_targets.append)
    monkeypatch.setattr(argocd, "reconcile_instance_application", successful_reconcile)
    tasks.step_02_create_instance.delay.reset_mock()

    assert tasks.step_01_create_schema.run(str(job_id)) == {"status": "pending"}
    assert len(created_targets) == 1
    assert created_targets[0].schema_name == f"coder_{instance_id.hex}"
    assert created_targets[0].password.get_secret_value() == "managed-secret"
    tasks.step_02_create_instance.delay.assert_called_once_with(str(job_id))

    async with session_maker() as session:
        job = await session.get(JobExecution, job_id)
        stored = await session.get(Instance, instance_id)
        assert job is not None
        assert stored is not None
        assert job.task_name == INSTANCE_CREATE_STEP_02_TASK
        assert job.step == INSTANCE_CREATE_STEP_02
        assert job.status is JobStatus.PENDING
        assert stored.step == INSTANCE_CREATE_STEP_02
        assert stored.status is InstanceStatus.PENDING

    assert tasks.step_02_create_instance.run(str(job_id)) == {"status": "success"}
    assert tasks.step_01_create_schema.run(str(job_id)) == {"status": "noop"}
    async with session_maker() as session:
        job = await session.get(JobExecution, job_id)
        stored = await session.get(Instance, instance_id)
        assert job is not None
        assert stored is not None
        assert job.status is JobStatus.SUCCESS
        assert job.attempt == 2
        assert stored.status is InstanceStatus.SUCCESS
        assert stored.step is None
        assert stored.argocd_application_name == f"coder-{instance_id.hex}"

    response = await client.get(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert (await client.get(f"/api/v1/jobs/{uuid4()}")).status_code == 404


async def test_create_failure_is_exactly_retryable_and_dispatch_loss_stays_pending(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist failures and let Beat recover a next step lost after commit."""

    configure_worker(monkeypatch, sync_session_maker)
    instance = await create_instance(client, "STEP FAILURE")
    instance_id = UUID(str(instance["id"]))
    job_id = UUID(str(instance["job_id"]))
    await encrypt_allocated_database(session_maker, instance_id)

    def fail_schema(_target: postgresql.SchemaTarget) -> None:
        """Simulate an unavailable managed PostgreSQL server."""

        raise RuntimeError("schema unavailable")

    monkeypatch.setattr(postgresql, "create_schema", fail_schema)
    with pytest.raises(RuntimeError, match="schema unavailable"):
        tasks.step_01_create_schema.run(str(job_id))
    async with session_maker() as session:
        job = await session.get(JobExecution, job_id)
        stored = await session.get(Instance, instance_id)
        assert job is not None
        assert stored is not None
        assert job.status is JobStatus.ERROR
        assert job.step == "step_01_create_schema"
        assert stored.status is InstanceStatus.ERROR

    monkeypatch.setattr(postgresql, "create_schema", lambda _target: None)
    tasks.step_02_create_instance.delay.side_effect = RuntimeError("redis unavailable")
    assert tasks.step_01_create_schema.run(str(job_id)) == {"status": "pending"}
    async with session_maker() as session:
        job = await session.get(JobExecution, job_id)
        assert job is not None
        assert job.status is JobStatus.PENDING
        assert job.task_name == INSTANCE_CREATE_STEP_02_TASK

    tasks.step_02_create_instance.delay.side_effect = None
    tasks.step_02_create_instance.delay.reset_mock()
    result = tasks.retry_job_executions.run()
    assert result["scheduled"] >= 1
    tasks.step_02_create_instance.delay.assert_any_call(str(job_id))


async def test_attempt_fencing_rejects_late_worker_completion(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
) -> None:
    """Prevent an expired attempt from completing after Beat has reclaimed it."""

    instance = await create_instance(client, "ATTEMPT FENCE")
    job_id = UUID(str(instance["job_id"]))
    first_claim = claim_execution(job_id, INSTANCE_CREATE_STEP_01_TASK, sync_session_maker)
    assert first_claim is not None
    stale_before = datetime.now(UTC) + timedelta(seconds=1)
    assert (
        prepare_execution_retry(
            job_id,
            stale_before=stale_before,
            session_factory=sync_session_maker,
        )
        == INSTANCE_CREATE_STEP_01_TASK
    )
    second_claim = claim_execution(job_id, INSTANCE_CREATE_STEP_01_TASK, sync_session_maker)
    assert second_claim is not None
    assert second_claim.attempt == first_claim.attempt + 1
    assert complete_execution(first_claim, sync_session_maker) is False
    assert complete_execution(second_claim, sync_session_maker) is True
    async with session_maker() as session:
        job = await session.get(JobExecution, job_id)
        assert job is not None
        assert job.status is JobStatus.SUCCESS


async def test_retried_update_reclaims_members_from_the_expired_attempt(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Let a new update attempt finish members left running by an expired worker."""

    configure_worker(monkeypatch, sync_session_maker)
    instance = await create_instance(client, "MEMBER ATTEMPT FENCE")
    instance_id = UUID(str(instance["id"]))
    await set_instance_status(session_maker, instance_id)
    response = await client.post(
        f"/api/v1/instances/{instance_id}/members",
        json={"username": "retry-member", "role": "user"},
    )
    job_id = UUID(response.json()["job"]["id"])
    first_claim = claim_execution(job_id, INSTANCE_UPDATE_STEP_01_TASK, sync_session_maker)
    assert first_claim is not None
    update_module = import_module("coder_manager.tasks.instance.update.step_01_update_instance")
    member_ids, *_ = update_module._claim_members(first_claim, sync_session_maker)
    assert len(member_ids) == 1
    assert (
        prepare_execution_retry(
            job_id,
            stale_before=datetime.now(UTC) + timedelta(seconds=1),
            session_factory=sync_session_maker,
        )
        == INSTANCE_UPDATE_STEP_01_TASK
    )

    monkeypatch.setattr(argocd, "reconcile_instance_application", successful_reconcile)
    assert tasks.step_01_update_instance.run(str(job_id)) == {"status": "success"}
    async with session_maker() as session:
        job = await session.get(JobExecution, job_id)
        member = await session.scalar(select(Member).where(Member.username == "retry-member"))
        assert job is not None
        assert member is not None
        assert job.attempt == first_claim.attempt + 1
        assert job.status is JobStatus.SUCCESS
        assert member.status is MemberStatus.SUCCESS


async def test_update_step_coalesces_member_changes_into_a_new_job(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finish one member snapshot and create a new job for changes arriving during it."""

    configure_worker(monkeypatch, sync_session_maker)
    instance = await create_instance(client, "MEMBER COALESCE")
    instance_id = UUID(str(instance["id"]))
    await set_instance_status(session_maker, instance_id)
    response = await client.post(
        f"/api/v1/instances/{instance_id}/members",
        json={"username": "first", "role": "user"},
    )
    assert response.status_code == 201
    first_job_id = UUID(response.json()["job"]["id"])

    def add_late_member(
        reconciled_id: UUID,
        attached_name: str | None,
        _members: tuple[tuple[str, str], ...],
        _region: str,
        _environment: str,
    ) -> str:
        """Insert a pending member while the first reconciliation is running."""

        with sync_session_maker() as session:
            session.add(Member(instance_id=reconciled_id, username="late", role="user"))
            session.commit()
        return attached_name or f"coder-{reconciled_id.hex}"

    monkeypatch.setattr(argocd, "reconcile_instance_application", add_late_member)
    tasks.step_01_update_instance.delay.reset_mock()
    assert tasks.step_01_update_instance.run(str(first_job_id)) == {"status": "pending"}
    async with session_maker() as session:
        instance_record = await session.get(Instance, instance_id)
        first_job = await session.get(JobExecution, first_job_id)
        assert instance_record is not None
        assert first_job is not None
        assert first_job.status is JobStatus.SUCCESS
        assert instance_record.job_id != first_job_id
        next_job_id = instance_record.job_id
        assert instance_record.step == INSTANCE_UPDATE_STEP_01
        first_member = await session.scalar(select(Member).where(Member.username == "first"))
        late_member = await session.scalar(select(Member).where(Member.username == "late"))
        assert first_member is not None
        assert late_member is not None
        assert first_member.status is MemberStatus.SUCCESS
        assert late_member.status is MemberStatus.PENDING
    assert next_job_id is not None
    tasks.step_01_update_instance.delay.assert_called_once_with(str(next_job_id))

    monkeypatch.setattr(argocd, "reconcile_instance_application", successful_reconcile)
    assert tasks.step_01_update_instance.run(str(next_job_id)) == {"status": "success"}
    async with session_maker() as session:
        late_member = await session.scalar(select(Member).where(Member.username == "late"))
        instance_record = await session.get(Instance, instance_id)
        assert late_member is not None
        assert instance_record is not None
        assert late_member.status is MemberStatus.SUCCESS
        assert instance_record.status is InstanceStatus.SUCCESS


async def test_delete_steps_keep_local_state_until_step_04(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute workspace, Argo CD, schema, and local deletion in strict order."""

    configure_worker(monkeypatch, sync_session_maker)
    instance, member, template, image = await create_ready_context(client, session_maker)
    instance_id = UUID(str(instance["id"]))
    await encrypt_allocated_database(session_maker, instance_id)
    workspace_response = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(instance, member, template, image),
    )
    workspace_id = UUID(workspace_response.json()["resource"]["id"])
    async with session_maker() as session:
        workspace = await session.get(Workspace, workspace_id)
        instance_record = await session.get(Instance, instance_id)
        assert workspace is not None
        assert instance_record is not None
        workspace.status = WorkspaceStatus.SUCCESS
        workspace.step = None
        instance_record.status = InstanceStatus.SUCCESS
        instance_record.step = None
        session.add(
            InstanceKubernetes(
                instance_id=instance_id,
                host="https://kubernetes.validation.invalid",
                namespace="validation",
                token_enc=b"encrypted-token",
                ca="validation-ca",
            )
        )
        await session.commit()

    deletion = await client.delete(f"/api/v1/instances/{instance_id}")
    job_id = UUID(deletion.json()["job"]["id"])
    deleted_remote: list[UUID] = []
    dropped_targets: list[postgresql.SchemaTarget] = []
    monkeypatch.setattr(
        argocd,
        "delete_instance_application",
        lambda remote_id, _name: deleted_remote.append(remote_id),
    )
    monkeypatch.setattr(postgresql, "drop_schema", dropped_targets.append)

    assert tasks.step_01_remove_workspaces.run(str(job_id)) == {"status": "pending"}
    assert tasks.step_02_remove_instance.run(str(job_id)) == {"status": "pending"}
    assert deleted_remote == [instance_id]
    assert tasks.step_03_remove_schema.run(str(job_id)) == {"status": "pending"}
    assert dropped_targets[0].schema_name == f"coder_{instance_id.hex}"
    async with session_maker() as session:
        assert await session.get(Instance, instance_id) is not None
        assert await session.get(Workspace, workspace_id) is not None
        job = await session.get(JobExecution, job_id)
        assert job is not None
        assert job.step == INSTANCE_DELETE_STEP_04
        assert job.task_name == INSTANCE_DELETE_STEP_04_TASK

    assert tasks.step_04_remove_local_configuration.run(str(job_id)) == {"status": "deleted"}
    async with session_maker() as session:
        assert await session.get(Instance, instance_id) is None
        assert await session.get(Workspace, workspace_id) is None
        assert await session.get(InstanceKubernetes, instance_id) is None
        job = await session.get(JobExecution, job_id)
        assert job is not None
        assert job.status is JobStatus.SUCCESS
        assert (
            await session.scalar(
                select(func.count())
                .select_from(DatabaseAllocation)
                .where(DatabaseAllocation.instance_id == instance_id)
            )
            == 0
        )


@pytest.mark.parametrize(
    ("failed_step", "expected_step"),
    [
        (1, "step_01_remove_workspaces"),
        (2, "step_02_remove_instance"),
        (3, "step_03_remove_schema"),
        (4, "step_04_remove_local_configuration"),
    ],
)
async def test_each_delete_step_failure_preserves_local_configuration(
    failed_step: int,
    expected_step: str,
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep every local dependent until all remote deletion steps have succeeded."""

    configure_worker(monkeypatch, sync_session_maker)
    instance, member, template, image = await create_ready_context(client, session_maker)
    instance_id = UUID(str(instance["id"]))
    member_id = UUID(str(member["id"]))
    await encrypt_allocated_database(session_maker, instance_id)
    workspace_response = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(instance, member, template, image),
    )
    workspace_id = UUID(workspace_response.json()["resource"]["id"])
    async with session_maker() as session:
        workspace = await session.get(Workspace, workspace_id)
        instance_record = await session.get(Instance, instance_id)
        assert workspace is not None
        assert instance_record is not None
        workspace.status = WorkspaceStatus.SUCCESS
        workspace.step = None
        instance_record.status = InstanceStatus.SUCCESS
        instance_record.step = None
        session.add(
            InstanceKubernetes(
                instance_id=instance_id,
                host="https://kubernetes.failure.invalid",
                namespace="failure-validation",
                token_enc=b"encrypted-token",
                ca="validation-ca",
            )
        )
        await session.commit()

    deletion = await client.delete(f"/api/v1/instances/{instance_id}")
    job_id = UUID(deletion.json()["job"]["id"])
    monkeypatch.setattr(argocd, "delete_instance_application", lambda *_args: None)
    monkeypatch.setattr(postgresql, "drop_schema", lambda _target: None)
    deletion_tasks = (
        tasks.step_01_remove_workspaces,
        tasks.step_02_remove_instance,
        tasks.step_03_remove_schema,
        tasks.step_04_remove_local_configuration,
    )
    for task in deletion_tasks[: failed_step - 1]:
        task.run(str(job_id))

    failed_module = import_module(deletion_tasks[failed_step - 1].run.__module__)

    def fail_step(*_args: object, **_kwargs: object) -> None:
        """Raise at the selected deletion boundary."""

        raise RuntimeError("selected deletion failure")

    if failed_step == 1:
        monkeypatch.setattr(failed_module, "placeholder", fail_step)
    elif failed_step == 2:
        monkeypatch.setattr(failed_module.argocd, "delete_instance_application", fail_step)
    elif failed_step == 3:
        monkeypatch.setattr(failed_module.postgresql, "drop_schema", fail_step)
    else:
        monkeypatch.setattr(failed_module, "owned_execution", fail_step)

    with pytest.raises(RuntimeError, match="selected deletion failure"):
        deletion_tasks[failed_step - 1].run(str(job_id))
    async with session_maker() as session:
        job = await session.get(JobExecution, job_id)
        instance_record = await session.get(Instance, instance_id)
        assert job is not None
        assert instance_record is not None
        assert job.status is JobStatus.ERROR
        assert job.step == expected_step
        assert instance_record.status is InstanceStatus.ERROR
        assert await session.get(Workspace, workspace_id) is not None
        assert await session.get(Member, member_id) is not None
        assert await session.get(InstanceKubernetes, instance_id) is not None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(DatabaseAllocation)
                .where(DatabaseAllocation.instance_id == instance_id)
            )
            == 1
        )


async def test_workspace_steps_and_database_sync_are_durable(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run each one-step workflow through its persisted job identifier."""

    configure_worker(monkeypatch, sync_session_maker)
    instance, member, template, image = await create_ready_context(client, session_maker)
    created = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(instance, member, template, image),
    )
    workspace = created.json()["resource"]
    create_job_id = UUID(created.json()["job"]["id"])
    assert tasks.step_01_create_workspace.run(str(create_job_id)) == {"status": "success"}

    updated = await client.put(
        f"/api/v1/workspaces/{workspace['id']}",
        json={
            "name": "updated",
            "image_id": image["id"],
            "modules": [],
            "cpu": 2,
            "ram": 8,
        },
    )
    update_job_id = UUID(updated.json()["job"]["id"])
    assert tasks.step_01_update_workspace.run(str(update_job_id)) == {"status": "success"}

    deleted = await client.delete(f"/api/v1/workspaces/{workspace['id']}")
    delete_job_id = UUID(deleted.json()["job"]["id"])
    assert tasks.step_01_delete_workspace.run(str(delete_job_id)) == {"status": "deleted"}
    async with session_maker() as session:
        assert await session.get(Workspace, UUID(str(workspace["id"]))) is None

    synced = await client.post("/api/v1/databases/sync")
    sync_job_id = UUID(synced.json()["job"]["id"])
    assert tasks.step_01_sync_database.run(str(sync_job_id)) == {"status": "success"}
    response = await client.get(f"/api/v1/jobs/{sync_job_id}")
    assert response.json()["status"] == "success"
    assert response.json()["resource_id"] is None


async def test_retry_scanner_handles_error_pending_stale_and_unknown_jobs(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recover exact known steps and safely skip an unknown persisted task name."""

    configure_worker(monkeypatch, sync_session_maker)
    job_ids = []
    for suffix in ("retry-error", "retry-stale", "retry-unknown"):
        instance = await create_instance(client, suffix)
        job_ids.append(UUID(str(instance["job_id"])))
    async with session_maker() as session:
        error_job = await session.get(JobExecution, job_ids[0])
        stale_job = await session.get(JobExecution, job_ids[1])
        unknown_job = await session.get(JobExecution, job_ids[2])
        assert error_job is not None
        assert stale_job is not None
        assert unknown_job is not None
        error_job.status = JobStatus.ERROR
        stale_job.status = JobStatus.RUNNING
        stale_job.claimed_at = datetime.now(UTC) - timedelta(hours=1)
        unknown_job.task_name = "coder_manager.unknown.step"
        await session.commit()

    tasks.step_01_create_schema.delay.reset_mock()
    result = tasks.retry_job_executions.run()
    assert result == {"status": "success", "scheduled": 2, "skipped": 1}
    assert tasks.step_01_create_schema.delay.call_count == 2
    assert dispatch_registered_step("coder_manager.unknown.step", uuid4()) is False


def test_postgresql_service_uses_quoted_idempotent_schema_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass secrets only to psycopg and quote CREATE/DROP schema identifiers."""

    connection = MagicMock()
    cursor = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    connect = MagicMock(return_value=connection)
    service = import_module("coder_manager.domains.postgresql.service")
    monkeypatch.setattr(service.psycopg, "connect", connect)
    target = postgresql.SchemaTarget(
        host="postgres.internal",
        port=5432,
        database_name="coder",
        username="manager",
        password=SecretStr("secret"),
        schema_name='coder_"quoted',
    )

    postgresql.create_schema(target)
    create_query = repr(cursor.execute.call_args.args[0])
    postgresql.drop_schema(target)
    drop_query = repr(cursor.execute.call_args.args[0])

    assert "CREATE SCHEMA IF NOT EXISTS" in create_query
    assert "Identifier" in create_query
    assert "DROP SCHEMA IF EXISTS" in drop_query
    assert "CASCADE" in drop_query
    assert connect.call_args.kwargs["password"] == "secret"
    assert connect.call_args.kwargs["connect_timeout"] == 5
