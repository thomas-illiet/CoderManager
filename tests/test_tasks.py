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
from coder_manager.crypto import (
    InstancePasswordCipher,
    KubeconfigCipher,
    KubeconfigDecryptionError,
    PasswordCipher,
)
from coder_manager.domains import argocd, coder, postgresql
from coder_manager.models import (
    Database,
    DatabaseAllocation,
    Instance,
    InstanceKubernetes,
    InstanceState,
    InstanceStatus,
    JobExecution,
    JobStatus,
    Member,
    MemberRole,
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
    INSTANCE_CREATE_STEP_03,
    INSTANCE_CREATE_STEP_03_TASK,
    INSTANCE_CREATE_STEP_04,
    INSTANCE_CREATE_STEP_04_TASK,
    INSTANCE_DELETE_STEP_04,
    INSTANCE_DELETE_STEP_04_TASK,
    INSTANCE_UPDATE_STEP_01,
    INSTANCE_UPDATE_STEP_01_TASK,
    INSTANCE_UPDATE_STEP_02,
    INSTANCE_UPDATE_STEP_02_TASK,
    REGISTERED_STEP_NAMES,
    dispatch_registered_step,
)
from coder_manager.tasks.instance import _bootstrap as bootstrap_helpers
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
    monkeypatch.setattr(
        bootstrap_helpers,
        "get_settings",
        lambda: Settings(crypto_key=TEST_CRYPTO_KEY),
    )
    cleanup_module = import_module("coder_manager.tasks.instance.update.step_02_cleanup_users")
    monkeypatch.setattr(
        cleanup_module,
        "get_settings",
        lambda: Settings(crypto_key=TEST_CRYPTO_KEY),
    )
    monkeypatch.setattr(
        coder,
        "cleanup_user_accounts",
        lambda _url, _password, _expected, *, heartbeat: heartbeat(),
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


async def store_admin_password(
    session_maker: async_sessionmaker[AsyncSession],
    instance_id: UUID,
) -> None:
    """Persist the administrator password expected by normal updates."""

    async with session_maker() as session:
        instance = await session.get(Instance, instance_id)
        assert instance is not None
        instance.password_enc = InstancePasswordCipher(SecretStr(TEST_CRYPTO_KEY)).encrypt(
            SecretStr("stored-admin-password"),
            instance_id,
        )
        await session.commit()


def successful_reconcile(
    _instance_id: UUID,
    slug: str,
    attached_name: str | None,
    _members: tuple[tuple[str, str], ...],
    _helm_values: argocd.InstanceHelmValues,
) -> str:
    """Return a deterministic Argo CD Application name."""

    return attached_name or f"coder-{slug}"


def test_registered_step_names_and_beat_schedule() -> None:
    """Register only explicit steps and the generic recovery control task."""

    assert {
        task.name
        for task in (
            tasks.step_01_create_schema,
            tasks.step_02_create_instance,
            tasks.step_03_bootstrap_admin,
            tasks.step_04_sync_templates,
            tasks.step_01_update_instance,
            tasks.step_02_cleanup_users,
            tasks.step_01_start_instance,
            tasks.step_01_stop_workspaces,
            tasks.step_02_stop_instance,
            tasks.step_01_remove_workspaces,
            tasks.step_02_remove_instance,
            tasks.step_03_remove_schema,
            tasks.step_04_remove_local_configuration,
            tasks.step_01_create_workspace,
            tasks.step_01_update_workspace,
            tasks.step_01_delete_workspace,
            tasks.step_01_sync_database,
            tasks.step_01_sync_template,
        )
    } == REGISTERED_STEP_NAMES
    assert not hasattr(tasks, "upsert_instance")
    schedule = celery_app.conf.beat_schedule["retry-job-executions"]
    assert schedule["task"] == "coder_manager.retry_job_executions"
    assert schedule["schedule"] == timedelta(seconds=get_settings().job_retry_interval_seconds)
    state_schedule = celery_app.conf.beat_schedule["check-instance-states"]
    assert state_schedule["task"] == "coder_manager.check_instance_states"
    assert state_schedule["schedule"] == timedelta(hours=1)
    task_source = Path(tasks.__file__).parent
    assert all("chain(" not in path.read_text() for path in task_source.rglob("*.py"))


async def test_start_and_stop_jobs_reconcile_workspaces_before_application_deletion(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start strictly, stop active workspaces, then delete only the Application."""

    configure_worker(monkeypatch, sync_session_maker)
    instance = await create_instance(client, "POWER JOBS")
    instance_id = UUID(str(instance["id"]))
    await encrypt_allocated_database(session_maker, instance_id)
    await store_admin_password(session_maker, instance_id)
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        stored.action = "updating"
        stored.status = InstanceStatus.SUCCESS
        stored.state = InstanceState.STOPPED
        stored.step = None
        await session.commit()

    monkeypatch.setattr(argocd, "reconcile_instance_application", successful_reconcile)
    started = await client.post(f"/api/v1/instances/{instance_id}/start")
    start_job_id = UUID(started.json()["job"]["id"])
    assert tasks.step_01_start_instance.run(str(start_job_id)) == {"status": "pending"}
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        assert stored.state is InstanceState.STARTED
        assert stored.status is InstanceStatus.PENDING
        assert stored.step == INSTANCE_UPDATE_STEP_02
    assert tasks.step_02_cleanup_users.run(str(start_job_id)) == {"status": "success"}

    events: list[str] = []
    monkeypatch.setattr(argocd, "instance_application_exists", lambda *_args: True)
    monkeypatch.setattr(
        coder,
        "stop_active_workspaces",
        lambda *_args, **_kwargs: events.append("workspaces"),
    )
    monkeypatch.setattr(
        argocd,
        "delete_instance_application",
        lambda *_args: events.append("application"),
    )
    stopped = await client.post(f"/api/v1/instances/{instance_id}/stop")
    stop_job_id = UUID(stopped.json()["job"]["id"])
    assert tasks.step_01_stop_workspaces.run(str(stop_job_id)) == {"status": "pending"}
    assert events == ["workspaces"]
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        assert stored.state is InstanceState.STARTED
        assert stored.step == "step_02_stop_instance"
        allocation = await session.scalar(
            select(DatabaseAllocation).where(DatabaseAllocation.instance_id == instance_id)
        )
        assert allocation is not None

    assert tasks.step_02_stop_instance.run(str(stop_job_id)) == {"status": "success"}
    assert events == ["workspaces", "application"]
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        assert stored.action == "stopping"
        assert stored.status is InstanceStatus.SUCCESS
        assert stored.state is InstanceState.STOPPED
        assert stored.step is None


async def test_stop_workspace_failure_keeps_application_and_started_state(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail safely before Application deletion when a workspace cannot stop."""

    configure_worker(monkeypatch, sync_session_maker)
    instance = await create_instance(client, "STOP FAILURE")
    instance_id = UUID(str(instance["id"]))
    await store_admin_password(session_maker, instance_id)
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        stored.action = "updating"
        stored.status = InstanceStatus.SUCCESS
        stored.state = InstanceState.STARTED
        stored.step = None
        await session.commit()

    deleted: list[str] = []
    monkeypatch.setattr(argocd, "instance_application_exists", lambda *_args: True)

    def fail_workspaces(*_args: object, **_kwargs: object) -> None:
        """Simulate one terminal remote workspace stop failure."""

        raise RuntimeError("workspace stop failed")

    monkeypatch.setattr(coder, "stop_active_workspaces", fail_workspaces)
    monkeypatch.setattr(
        argocd,
        "delete_instance_application",
        lambda *_args: deleted.append("application"),
    )
    response = await client.post(f"/api/v1/instances/{instance_id}/stop")
    job_id = UUID(response.json()["job"]["id"])

    with pytest.raises(RuntimeError, match="workspace stop failed"):
        tasks.step_01_stop_workspaces.run(str(job_id))
    assert deleted == []
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        job = await session.get(JobExecution, job_id)
        assert stored is not None
        assert job is not None
        assert stored.state is InstanceState.STARTED
        assert stored.status is InstanceStatus.ERROR
        assert stored.step == "step_01_stop_workspaces"
        assert job.status is JobStatus.ERROR


async def test_stop_already_absent_skips_coder_and_converges(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Converge an absent Application without requiring Coder credentials."""

    configure_worker(monkeypatch, sync_session_maker)
    instance = await create_instance(client, "STOP ABSENT")
    instance_id = UUID(str(instance["id"]))
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        stored.action = "updating"
        stored.status = InstanceStatus.SUCCESS
        stored.state = InstanceState.STARTED
        stored.step = None
        assert stored.password_enc is None
        await session.commit()

    monkeypatch.setattr(argocd, "instance_application_exists", lambda *_args: False)
    monkeypatch.setattr(
        coder,
        "stop_active_workspaces",
        lambda *_args, **_kwargs: pytest.fail("Coder must not be called"),
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        argocd,
        "delete_instance_application",
        lambda *_args: deleted.append("idempotent-delete"),
    )
    response = await client.post(f"/api/v1/instances/{instance_id}/stop")
    job_id = UUID(response.json()["job"]["id"])

    assert tasks.step_01_stop_workspaces.run(str(job_id)) == {"status": "pending"}
    assert tasks.step_02_stop_instance.run(str(job_id)) == {"status": "success"}
    assert deleted == ["idempotent-delete"]
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        assert stored.state is InstanceState.STOPPED
        assert stored.status is InstanceStatus.SUCCESS


async def test_start_fails_without_admin_credentials(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail a strict start before Argo reconciliation when credentials are absent."""

    configure_worker(monkeypatch, sync_session_maker)
    instance = await create_instance(client, "START CREDENTIALS")
    instance_id = UUID(str(instance["id"]))
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        stored.action = "updating"
        stored.status = InstanceStatus.SUCCESS
        stored.step = None
        assert stored.password_enc is None
        await session.commit()

    monkeypatch.setattr(
        argocd,
        "reconcile_instance_application",
        lambda *_args: pytest.fail("Argo must not be called"),
    )
    response = await client.post(f"/api/v1/instances/{instance_id}/start")
    job_id = UUID(response.json()["job"]["id"])

    with pytest.raises(RuntimeError, match="administrator password is missing"):
        tasks.step_01_start_instance.run(str(job_id))
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        job = await session.get(JobExecution, job_id)
        assert stored is not None
        assert job is not None
        assert stored.state is InstanceState.STOPPED
        assert stored.status is InstanceStatus.ERROR
        assert job.status is JobStatus.ERROR


async def test_hourly_state_audit_observes_idle_instances_and_isolates_errors(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observe present and absent Applications while skipping busy instances."""

    configure_worker(monkeypatch, sync_session_maker)
    records = [await create_instance(client, f"AUDIT {index}") for index in range(4)]
    ids = [UUID(str(record["id"])) for record in records]
    async with session_maker() as session:
        stored = [await session.get(Instance, instance_id) for instance_id in ids]
        assert all(instance is not None for instance in stored)
        for instance in stored[:3]:
            assert instance is not None
            instance.action = "updating"
            instance.status = InstanceStatus.SUCCESS
            instance.step = None
        assert stored[0] is not None
        assert stored[1] is not None
        assert stored[2] is not None
        assert stored[3] is not None
        stored[0].state = InstanceState.STOPPED
        stored[1].state = InstanceState.STARTED
        stored[2].state = InstanceState.STARTED
        stored[3].action = "starting"
        stored[3].status = InstanceStatus.PENDING
        await session.commit()

    slug_results = {
        records[0]["slug"]: True,
        records[1]["slug"]: False,
    }

    def observe(slug: str, _attached_name: str | None) -> bool:
        """Return two observations and fail one independently."""

        if slug == records[2]["slug"]:
            raise RuntimeError("Argo unavailable")
        if slug == records[3]["slug"]:
            pytest.fail("busy instance must not be observed")
        return slug_results[slug]

    monkeypatch.setattr(argocd, "instance_application_exists", observe)
    result = tasks.check_instance_states.run()
    assert result == {"checked": 2, "changed": 2, "errors": 1}
    async with session_maker() as session:
        observed = [await session.get(Instance, instance_id) for instance_id in ids]
        assert all(instance is not None for instance in observed)
        assert observed[0].state is InstanceState.STARTED
        assert observed[1].state is InstanceState.STOPPED
        assert observed[2].state is InstanceState.STARTED
        assert observed[3].state is InstanceState.STOPPED


async def test_hourly_state_audit_discards_concurrent_lifecycle_change(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discard a remote observation when lifecycle ownership changes mid-request."""

    configure_worker(monkeypatch, sync_session_maker)
    record = await create_instance(client, "AUDIT FENCE")
    instance_id = UUID(str(record["id"]))
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        stored.action = "updating"
        stored.status = InstanceStatus.SUCCESS
        stored.state = InstanceState.STOPPED
        stored.step = None
        await session.commit()

    def observe(_slug: str, _attached_name: str | None) -> bool:
        """Change lifecycle state while the Argo observation is in flight."""

        with sync_session_maker() as session:
            stored = session.get(Instance, instance_id)
            assert stored is not None
            stored.action = "starting"
            stored.status = InstanceStatus.PENDING
            session.commit()
        return True

    monkeypatch.setattr(argocd, "instance_application_exists", observe)
    assert tasks.check_instance_states.run() == {"checked": 1, "changed": 0, "errors": 0}
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        assert stored.action == "starting"
        assert stored.status is InstanceStatus.PENDING
        assert stored.state is InstanceState.STOPPED


async def test_create_steps_advance_after_commit_and_finish_instance(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create the schema and Argo CD resource before bootstrapping Coder."""

    configure_worker(monkeypatch, sync_session_maker)
    instance = await create_instance(client, "STEP CREATE")
    instance_id = UUID(str(instance["id"]))
    job_id = UUID(str(instance["job_id"]))
    await encrypt_allocated_database(session_maker, instance_id)
    created_targets: list[postgresql.SchemaTarget] = []
    reconciled_values: list[argocd.InstanceHelmValues] = []
    bootstrapped: list[tuple[str, SecretStr]] = []

    def capture_reconcile(
        remote_id: UUID,
        slug: str,
        attached_name: str | None,
        members: tuple[tuple[str, str], ...],
        helm_values: argocd.InstanceHelmValues,
    ) -> str:
        """Capture the dynamic Helm values passed to Argo CD."""

        reconciled_values.append(helm_values)
        return successful_reconcile(
            remote_id,
            slug,
            attached_name,
            members,
            helm_values,
        )

    monkeypatch.setattr(postgresql, "create_schema", created_targets.append)
    monkeypatch.setattr(argocd, "reconcile_instance_application", capture_reconcile)
    monkeypatch.setattr(
        coder,
        "bootstrap_admin_account",
        lambda url, password: bootstrapped.append((url, password)),
    )
    tasks.step_02_create_instance.delay.reset_mock()
    tasks.step_03_bootstrap_admin.delay.reset_mock()
    tasks.step_04_sync_templates.delay.reset_mock()

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

    assert tasks.step_02_create_instance.run(str(job_id)) == {"status": "pending"}
    assert len(reconciled_values) == 1
    assert reconciled_values[0].public_url == instance["instance_url"]
    assert reconciled_values[0].base_domain == str(instance["instance_url"]).removeprefix(
        "https://"
    )
    assert reconciled_values[0].wildcard_access_host == (
        f"*.{str(instance['instance_url']).removeprefix('https://')}"
    )
    assert reconciled_values[0].database_username == "coder_manager"
    assert reconciled_values[0].database_password.get_secret_value() == "managed-secret"
    assert reconciled_values[0].database_host == "postgres.internal"
    assert reconciled_values[0].database_name == "coder"
    assert reconciled_values[0].managed_database_name == "test"
    assert reconciled_values[0].database_schema == f"coder_{instance_id.hex}"
    tasks.step_03_bootstrap_admin.delay.assert_called_once_with(str(job_id))
    async with session_maker() as session:
        job = await session.get(JobExecution, job_id)
        stored = await session.get(Instance, instance_id)
        assert job is not None
        assert stored is not None
        assert job.task_name == INSTANCE_CREATE_STEP_03_TASK
        assert job.step == INSTANCE_CREATE_STEP_03
        assert job.status is JobStatus.PENDING
        assert stored.step == INSTANCE_CREATE_STEP_03
        assert stored.status is InstanceStatus.PENDING
        assert stored.argocd_application_name == f"coder-{instance['slug']}"

    assert tasks.step_03_bootstrap_admin.run(str(job_id)) == {"status": "pending"}
    assert len(bootstrapped) == 1
    assert bootstrapped[0][0] == instance["instance_url"]
    assert len(bootstrapped[0][1].get_secret_value()) == 43
    tasks.step_04_sync_templates.delay.assert_called_once_with(str(job_id))
    async with session_maker() as session:
        job = await session.get(JobExecution, job_id)
        stored = await session.get(Instance, instance_id)
        assert job is not None
        assert stored is not None
        assert job.task_name == INSTANCE_CREATE_STEP_04_TASK
        assert job.step == INSTANCE_CREATE_STEP_04
        assert job.status is JobStatus.PENDING
        assert stored.step == INSTANCE_CREATE_STEP_04
        assert stored.status is InstanceStatus.PENDING

    assert tasks.step_04_sync_templates.run(str(job_id)) == {"status": "success"}
    assert tasks.step_01_create_schema.run(str(job_id)) == {"status": "noop"}
    async with session_maker() as session:
        job = await session.get(JobExecution, job_id)
        stored = await session.get(Instance, instance_id)
        assert job is not None
        assert stored is not None
        assert job.status is JobStatus.SUCCESS
        assert job.attempt == 4
        assert stored.status is InstanceStatus.SUCCESS
        assert stored.step is None
        assert stored.argocd_application_name == f"coder-{instance['slug']}"
        assert stored.password_enc is not None
        assert bootstrapped[0][1].get_secret_value().encode() not in stored.password_enc
        assert (
            InstancePasswordCipher(SecretStr(TEST_CRYPTO_KEY))
            .decrypt(stored.password_enc, instance_id)
            .get_secret_value()
            == bootstrapped[0][1].get_secret_value()
        )

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


async def test_bootstrap_stores_password_only_after_success_and_never_reprocesses_it(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist only verified credentials and short-circuit redundant bootstrap jobs."""

    configure_worker(monkeypatch, sync_session_maker)
    instance = await create_instance(client, "BOOTSTRAP RETRY")
    instance_id = UUID(str(instance["id"]))
    job_id = UUID(str(instance["job_id"]))
    with sync_session_maker() as session:
        job = session.get(JobExecution, job_id)
        stored = session.get(Instance, instance_id)
        assert job is not None
        assert stored is not None
        job.task_name = INSTANCE_CREATE_STEP_03_TASK
        job.step = INSTANCE_CREATE_STEP_03
        stored.step = INSTANCE_CREATE_STEP_03
        session.commit()

    observed_passwords: list[str] = []

    def fail_once(_url: str, password: SecretStr) -> None:
        """Capture the prepared password and simulate one remote failure."""

        observed_passwords.append(password.get_secret_value())
        if len(observed_passwords) == 1:
            raise RuntimeError("Coder unavailable")

    monkeypatch.setattr(coder, "bootstrap_admin_account", fail_once)
    with pytest.raises(RuntimeError, match="Coder unavailable"):
        tasks.step_03_bootstrap_admin.run(str(job_id))
    async with session_maker() as session:
        failed_job = await session.get(JobExecution, job_id)
        failed_instance = await session.get(Instance, instance_id)
        assert failed_job is not None
        assert failed_instance is not None
        assert failed_job.status is JobStatus.ERROR
        assert failed_instance.status is InstanceStatus.ERROR
        assert failed_instance.password_enc is None

    assert tasks.step_03_bootstrap_admin.run(str(job_id)) == {"status": "pending"}
    assert len(observed_passwords) == 2
    assert observed_passwords[0] != observed_passwords[1]
    async with session_maker() as session:
        bootstrapped_instance = await session.get(Instance, instance_id)
        assert bootstrapped_instance is not None
        assert bootstrapped_instance.password_enc is not None
        assert (
            InstancePasswordCipher(SecretStr(TEST_CRYPTO_KEY))
            .decrypt(bootstrapped_instance.password_enc, instance_id)
            .get_secret_value()
            == observed_passwords[1]
        )
    assert tasks.step_04_sync_templates.run(str(job_id)) == {"status": "success"}

    redundant_job_id = uuid4()
    with sync_session_maker() as session:
        stored = session.get(Instance, instance_id)
        assert stored is not None
        session.add(
            JobExecution(
                id=redundant_job_id,
                name="instance.create",
                task_name=INSTANCE_CREATE_STEP_03_TASK,
                resource_type="instance",
                resource_id=instance_id,
                step=INSTANCE_CREATE_STEP_03,
                status=JobStatus.PENDING,
            )
        )
        stored.action = "creating"
        stored.status = InstanceStatus.PENDING
        stored.job_id = redundant_job_id
        stored.step = INSTANCE_CREATE_STEP_03
        session.commit()

    monkeypatch.setattr(
        coder,
        "bootstrap_admin_account",
        lambda _url, _password: pytest.fail("remote bootstrap must not be called"),
    )
    assert tasks.step_03_bootstrap_admin.run(str(redundant_job_id)) == {"status": "pending"}
    assert tasks.step_04_sync_templates.run(str(redundant_job_id)) == {"status": "success"}


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
    await encrypt_allocated_database(session_maker, instance_id)
    await set_instance_status(session_maker, instance_id)
    await store_admin_password(session_maker, instance_id)
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
    assert tasks.step_01_update_instance.run(str(job_id)) == {"status": "pending"}
    async with session_maker() as session:
        advanced_job = await session.get(JobExecution, job_id)
        assert advanced_job is not None
        assert advanced_job.attempt == first_claim.attempt + 1
        assert advanced_job.task_name == INSTANCE_UPDATE_STEP_02_TASK
        assert advanced_job.step == INSTANCE_UPDATE_STEP_02
        assert advanced_job.status is JobStatus.PENDING

    assert tasks.step_02_cleanup_users.run(str(job_id)) == {"status": "success"}
    async with session_maker() as session:
        job = await session.get(JobExecution, job_id)
        member = await session.scalar(select(Member).where(Member.username == "retry-member"))
        assert job is not None
        assert member is not None
        assert job.attempt == first_claim.attempt + 2
        assert job.status is JobStatus.SUCCESS
        assert member.status is MemberStatus.SUCCESS


async def test_update_fails_without_admin_credentials(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject an update with no administrator credentials and no bootstrap fallback."""

    configure_worker(monkeypatch, sync_session_maker)
    instance = await create_instance(client, "EXISTING ADMIN BACKFILL")
    instance_id = UUID(str(instance["id"]))
    await encrypt_allocated_database(session_maker, instance_id)
    await set_instance_status(session_maker, instance_id)
    kubeconfig = b"\x00worker-kubeconfig\xff"
    async with session_maker() as session:
        session.add(
            InstanceKubernetes(
                instance_id=instance_id,
                kubeconfig_enc=KubeconfigCipher(SecretStr(TEST_CRYPTO_KEY)).encrypt(
                    kubeconfig,
                    instance_id,
                ),
            )
        )
        await session.commit()
    response = await client.post(f"/api/v1/instances/{instance_id}/sync")
    job_id = UUID(response.json()["job"]["id"])
    reconciled_values: list[argocd.InstanceHelmValues] = []

    def capture_reconcile(
        remote_id: UUID,
        slug: str,
        attached_name: str | None,
        members: tuple[tuple[str, str], ...],
        helm_values: argocd.InstanceHelmValues,
    ) -> str:
        """Capture the decrypted kubeconfig passed to the Argo CD boundary."""

        reconciled_values.append(helm_values)
        return successful_reconcile(remote_id, slug, attached_name, members, helm_values)

    monkeypatch.setattr(argocd, "reconcile_instance_application", capture_reconcile)
    tasks.step_02_cleanup_users.delay.reset_mock()
    tasks.step_03_bootstrap_admin.delay.reset_mock()

    assert tasks.step_01_update_instance.run(str(job_id)) == {"status": "pending"}
    assert len(reconciled_values) == 1
    assert reconciled_values[0].kubeconfig is not None
    assert reconciled_values[0].kubeconfig.get_secret_value() == kubeconfig
    tasks.step_02_cleanup_users.delay.assert_called_once_with(str(job_id))
    tasks.step_03_bootstrap_admin.delay.assert_not_called()
    async with session_maker() as session:
        job = await session.get(JobExecution, job_id)
        stored = await session.get(Instance, instance_id)
        assert job is not None
        assert stored is not None
        assert job.task_name == INSTANCE_UPDATE_STEP_02_TASK
        assert job.step == INSTANCE_UPDATE_STEP_02
        assert job.status is JobStatus.PENDING
        assert stored.job_id == job_id
        assert stored.step == INSTANCE_UPDATE_STEP_02
        assert stored.status is InstanceStatus.PENDING

    with pytest.raises(RuntimeError, match="administrator password is missing"):
        tasks.step_02_cleanup_users.run(str(job_id))
    async with session_maker() as session:
        job = await session.get(JobExecution, job_id)
        stored = await session.get(Instance, instance_id)
        assert job is not None
        assert stored is not None
        assert job.status is JobStatus.ERROR
        assert stored.status is InstanceStatus.ERROR
        assert stored.password_enc is None


async def test_update_fails_when_kubeconfig_cannot_be_decrypted(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mark the job and instance as errors when kubeconfig authentication fails."""

    configure_worker(monkeypatch, sync_session_maker)
    instance = await create_instance(client, "INVALID KUBECONFIG")
    instance_id = UUID(str(instance["id"]))
    await encrypt_allocated_database(session_maker, instance_id)
    await set_instance_status(session_maker, instance_id)
    async with session_maker() as session:
        session.add(
            InstanceKubernetes(
                instance_id=instance_id,
                kubeconfig_enc=b"invalid-envelope",
            )
        )
        await session.commit()
    response = await client.post(f"/api/v1/instances/{instance_id}/sync")
    job_id = UUID(response.json()["job"]["id"])

    with pytest.raises(KubeconfigDecryptionError):
        tasks.step_01_update_instance.run(str(job_id))

    async with session_maker() as session:
        job = await session.get(JobExecution, job_id)
        stored = await session.get(Instance, instance_id)
        assert job is not None
        assert stored is not None
        assert job.status is JobStatus.ERROR
        assert stored.status is InstanceStatus.ERROR


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
    await encrypt_allocated_database(session_maker, instance_id)
    await set_instance_status(session_maker, instance_id)
    await store_admin_password(session_maker, instance_id)
    response = await client.post(
        f"/api/v1/instances/{instance_id}/members",
        json={"username": "first", "role": "user"},
    )
    assert response.status_code == 201
    first_job_id = UUID(response.json()["job"]["id"])
    cleanup_snapshots: list[frozenset[str]] = []

    def capture_cleanup_snapshot(
        _instance_url: str,
        _password: SecretStr,
        expected_usernames: tuple[str, ...],
        *,
        heartbeat: object,
    ) -> tuple[str, ...]:
        """Capture references seen after changes coalesced during reconciliation."""

        cleanup_snapshots.append(frozenset(expected_usernames))
        assert callable(heartbeat)
        heartbeat()
        return ()

    def add_late_member(
        reconciled_id: UUID,
        slug: str,
        attached_name: str | None,
        _members: tuple[tuple[str, str], ...],
        _helm_values: argocd.InstanceHelmValues,
    ) -> str:
        """Insert a pending member while the first reconciliation is running."""

        with sync_session_maker() as session:
            session.add(Member(instance_id=reconciled_id, username="late", role="user"))
            session.commit()
        return attached_name or f"coder-{slug}"

    monkeypatch.setattr(argocd, "reconcile_instance_application", add_late_member)
    monkeypatch.setattr(coder, "cleanup_user_accounts", capture_cleanup_snapshot)
    tasks.step_01_update_instance.delay.reset_mock()
    tasks.step_02_cleanup_users.delay.reset_mock()
    assert tasks.step_01_update_instance.run(str(first_job_id)) == {"status": "pending"}
    tasks.step_02_cleanup_users.delay.assert_called_once_with(str(first_job_id))
    assert tasks.step_02_cleanup_users.run(str(first_job_id)) == {"status": "pending"}
    assert cleanup_snapshots == [frozenset({"admin", "first", "late"})]
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
    assert tasks.step_01_update_instance.run(str(next_job_id)) == {"status": "pending"}
    assert tasks.step_02_cleanup_users.run(str(next_job_id)) == {"status": "success"}
    assert cleanup_snapshots == [
        frozenset({"admin", "first", "late"}),
        frozenset({"admin", "first", "late"}),
    ]
    async with session_maker() as session:
        late_member = await session.scalar(select(Member).where(Member.username == "late"))
        instance_record = await session.get(Instance, instance_id)
        assert late_member is not None
        assert instance_record is not None
        assert late_member.status is MemberStatus.SUCCESS
        assert instance_record.status is InstanceStatus.SUCCESS


async def test_update_deletes_remote_accounts_before_local_members(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revoke Argo CD access, delete a batch in Coder, then remove local rows."""

    configure_worker(monkeypatch, sync_session_maker)
    cleanup_module = import_module("coder_manager.tasks.instance.update.step_02_cleanup_users")
    monkeypatch.setattr(
        cleanup_module,
        "get_settings",
        lambda: Settings(
            crypto_key=TEST_CRYPTO_KEY,
            default_admins=" Root.Admin ",
        ),
    )
    instance = await create_instance(client, "REMOTE MEMBER DELETE")
    instance_id = UUID(str(instance["id"]))
    await encrypt_allocated_database(session_maker, instance_id)
    await set_instance_status(session_maker, instance_id)
    await store_admin_password(session_maker, instance_id)
    async with session_maker() as session:
        members = [
            Member(
                instance_id=instance_id,
                username=username,
                role=MemberRole.USER,
                action="creating",
                status=MemberStatus.SUCCESS,
            )
            for username in ("alice", "bob", "root.admin")
        ]
        session.add_all(members)
        await session.commit()
        member_ids = {member.username: member.id for member in members}

    first = await client.delete(f"/api/v1/instances/{instance_id}/members/{member_ids['alice']}")
    second = await client.delete(f"/api/v1/instances/{instance_id}/members/{member_ids['bob']}")
    assert first.status_code == 202
    assert second.status_code == 202
    job_id = UUID(first.json()["job"]["id"])
    async with session_maker() as session:
        protected = await session.get(Member, member_ids["root.admin"])
        assert protected is not None
        protected.action = "deleting"
        protected.status = MemberStatus.PENDING
        await session.commit()

    events: list[tuple[str, object]] = []

    def capture_reconcile(
        remote_id: UUID,
        slug: str,
        attached_name: str | None,
        members: tuple[tuple[str, str], ...],
        helm_values: argocd.InstanceHelmValues,
    ) -> str:
        """Record that policy reconciliation precedes account deletion."""

        events.append(("argocd", members))
        return successful_reconcile(
            remote_id,
            slug,
            attached_name,
            members,
            helm_values,
        )

    def capture_cleanup(
        _instance_url: str,
        _password: SecretStr,
        expected_usernames: tuple[str, ...],
        *,
        heartbeat: object,
    ) -> tuple[str, ...]:
        """Record the expected set and exercise the cleanup heartbeat."""

        events.append(("coder", frozenset(expected_usernames)))
        assert callable(heartbeat)
        heartbeat()
        return ("alice", "bob")

    monkeypatch.setattr(argocd, "reconcile_instance_application", capture_reconcile)
    monkeypatch.setattr(coder, "cleanup_user_accounts", capture_cleanup)

    assert tasks.step_01_update_instance.run(str(job_id)) == {"status": "pending"}
    assert tasks.step_02_cleanup_users.run(str(job_id)) == {"status": "success"}

    assert events == [
        ("argocd", ()),
        ("coder", frozenset({"admin", "root.admin"})),
    ]
    async with session_maker() as session:
        remaining = list(
            await session.scalars(select(Member).where(Member.instance_id == instance_id))
        )
        job = await session.get(JobExecution, job_id)
        stored_instance = await session.get(Instance, instance_id)
        assert remaining == []
        assert job is not None
        assert job.status is JobStatus.SUCCESS
        assert stored_instance is not None
        assert stored_instance.status is InstanceStatus.SUCCESS


async def test_failed_remote_account_deletion_is_retried_before_local_removal(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep failed deletion state, then converge safely on the next attempt."""

    configure_worker(monkeypatch, sync_session_maker)
    instance = await create_instance(client, "REMOTE MEMBER RETRY")
    instance_id = UUID(str(instance["id"]))
    await encrypt_allocated_database(session_maker, instance_id)
    await set_instance_status(session_maker, instance_id)
    await store_admin_password(session_maker, instance_id)
    async with session_maker() as session:
        member = Member(
            instance_id=instance_id,
            username="alice",
            role=MemberRole.USER,
            action="creating",
            status=MemberStatus.SUCCESS,
        )
        session.add(member)
        await session.commit()
        member_id = member.id

    deletion = await client.delete(f"/api/v1/instances/{instance_id}/members/{member_id}")
    job_id = UUID(deletion.json()["job"]["id"])
    attempts = 0

    def cleanup_with_transient_failure(
        _instance_url: str,
        _password: SecretStr,
        _expected_usernames: tuple[str, ...],
        *,
        heartbeat: object,
    ) -> tuple[str, ...]:
        """Fail the first remote attempt and complete the retry."""

        nonlocal attempts
        attempts += 1
        assert callable(heartbeat)
        heartbeat()
        if attempts == 1:
            raise coder.CoderRequestError("Coder DELETE returned HTTP 417")
        return ("alice",)

    monkeypatch.setattr(argocd, "reconcile_instance_application", successful_reconcile)
    monkeypatch.setattr(coder, "cleanup_user_accounts", cleanup_with_transient_failure)

    assert tasks.step_01_update_instance.run(str(job_id)) == {"status": "pending"}
    with pytest.raises(coder.CoderRequestError):
        tasks.step_02_cleanup_users.run(str(job_id))

    async with session_maker() as session:
        failed_member = await session.get(Member, member_id)
        failed_job = await session.get(JobExecution, job_id)
        failed_instance = await session.get(Instance, instance_id)
        assert failed_member is not None
        assert failed_member.action == "deleting"
        assert failed_member.status is MemberStatus.ERROR
        assert failed_job is not None
        assert failed_job.status is JobStatus.ERROR
        assert failed_instance is not None
        assert failed_instance.status is InstanceStatus.ERROR

    assert tasks.step_02_cleanup_users.run(str(job_id)) == {"status": "success"}
    assert attempts == 2
    async with session_maker() as session:
        assert await session.get(Member, member_id) is None
        retried_job = await session.get(JobExecution, job_id)
        retried_instance = await session.get(Instance, instance_id)
        assert retried_job is not None
        assert retried_job.status is JobStatus.SUCCESS
        assert retried_job.attempt == 3
        assert retried_instance is not None
        assert retried_instance.status is InstanceStatus.SUCCESS


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
                kubeconfig_enc=b"encrypted-kubeconfig",
            )
        )
        await session.commit()

    deletion = await client.delete(f"/api/v1/instances/{instance_id}")
    job_id = UUID(deletion.json()["job"]["id"])
    deleted_remote: list[tuple[UUID, str | None, str | None]] = []
    dropped_targets: list[postgresql.SchemaTarget] = []
    monkeypatch.setattr(
        argocd,
        "delete_instance_application",
        lambda slug, name: deleted_remote.append((instance_id, slug, name)),
    )
    monkeypatch.setattr(postgresql, "drop_schema", dropped_targets.append)

    assert tasks.step_01_remove_workspaces.run(str(job_id)) == {"status": "pending"}
    assert tasks.step_02_remove_instance.run(str(job_id)) == {"status": "pending"}
    assert deleted_remote == [(instance_id, str(deletion.json()["resource"]["slug"]), None)]
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
                kubeconfig_enc=b"encrypted-kubeconfig",
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
