"""Celery lifecycle task tests."""

# ruff: noqa: EM101, PLR0915, SLF001, TRY003

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from coder_manager import tasks, worker_database
from coder_manager.domains import argocd
from coder_manager.models import (
    DatabaseAllocation,
    Instance,
    InstanceStatus,
    Member,
    MemberStatus,
    Workspace,
)
from coder_manager.tasks import _common as task_common
from coder_manager.tasks import healthcheck
from tests.test_workspaces import (
    create_application,
    create_instance,
    create_ready_context,
    set_instance_status,
    workspace_payload,
)


def successful_reconcile(
    instance_id: UUID,
    attached_name: str | None,
    _members: tuple[tuple[str, str], ...],
    _region: str,
    _environment: str,
) -> str:
    """Return a deterministic Argo CD name without making a network request."""

    return attached_name or f"coder-{instance_id.hex}"


def test_worker_healthcheck_and_registered_names() -> None:
    """Verify the worker healthcheck and registered names scenario."""

    assert healthcheck.run() == {"status": "ok"}
    assert tasks.sync_database.run() == {"status": "success"}
    assert {
        tasks.create_instance.name,
        tasks.update_instance.name,
        tasks.delete_instance.name,
        tasks.create_workspace.name,
        tasks.update_workspace.name,
        tasks.delete_workspace.name,
        tasks.sync_database.name,
    } == {
        "coder_manager.create_instance",
        "coder_manager.update_instance",
        "coder_manager.delete_instance",
        "coder_manager.create_workspace",
        "coder_manager.update_workspace",
        "coder_manager.delete_workspace",
        "coder_manager.sync_database",
    }


async def test_create_instance_success_duplicate_and_error(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
) -> None:
    """Verify the create instance success duplicate and error scenario."""

    application = await create_application(client)
    instance = await create_instance(client, application["id"])
    instance_id = UUID(str(instance["id"]))

    result = tasks._create_instance(
        instance_id,
        sync_session_maker,
        successful_reconcile,
    )
    duplicate = tasks._create_instance(
        instance_id,
        sync_session_maker,
        successful_reconcile,
    )
    assert result == {"status": "success"}
    assert duplicate == {"status": "noop"}
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        assert stored.argocd_application_name == f"coder-{instance_id.hex}"

    second_application = await create_application(client, suffix="error")
    failed = await create_instance(client, second_application["id"])
    failed_id = UUID(str(failed["id"]))

    def failing_reconcile(
        _instance_id: UUID,
        _attached_name: str | None,
        _members: tuple[tuple[str, str], ...],
        _region: str,
        _environment: str,
    ) -> str:
        """Simulate the expected failing reconcile behavior."""

        raise RuntimeError("Argo CD failed")

    with pytest.raises(RuntimeError, match="Argo CD failed"):
        tasks._create_instance(
            failed_id,
            sync_session_maker,
            failing_reconcile,
        )
    async with session_maker() as session:
        stored = await session.get(Instance, failed_id)
        assert stored is not None
        assert stored.status is InstanceStatus.ERROR


async def test_celery_failure_guard_marks_active_database_states_as_error(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist terminal errors even when a task fails outside its lifecycle helper."""

    monkeypatch.setattr(
        worker_database,
        "get_worker_session_maker",
        lambda: sync_session_maker,
    )
    application = await create_application(client, suffix="failure-guard")
    instance = await create_instance(client, application["id"])
    instance_id = UUID(str(instance["id"]))

    tasks.create_instance.on_failure(
        RuntimeError("worker failed"),
        "create-task-id",
        (str(instance_id),),
        {},
        None,
    )
    async with session_maker() as session:
        stored_instance = await session.get(Instance, instance_id)
        assert stored_instance is not None
        assert stored_instance.status is InstanceStatus.ERROR

        stored_instance.action = "updating"
        stored_instance.status = InstanceStatus.RUNNING
        running_member = Member(
            instance_id=instance_id,
            username="claimed-member",
            role="user",
            action="updating",
            status=MemberStatus.RUNNING,
        )
        session.add(running_member)
        await session.commit()
        member_id = running_member.id

    tasks.update_instance.on_failure(
        RuntimeError("Argo CD failed"),
        "update-task-id",
        (str(instance_id),),
        {},
        None,
    )
    async with session_maker() as session:
        stored_instance = await session.get(Instance, instance_id)
        stored_member = await session.get(Member, member_id)
        assert stored_instance is not None
        assert stored_instance.status is InstanceStatus.ERROR
        assert stored_member is not None
        assert stored_member.status is MemberStatus.ERROR

        stored_instance.status = InstanceStatus.RUNNING
        await session.commit()

    tasks.update_instance.on_failure(
        RuntimeError("forced sync failed"),
        "forced-update-task-id",
        (str(instance_id),),
        {"force": True},
        None,
    )
    async with session_maker() as session:
        stored_instance = await session.get(Instance, instance_id)
        assert stored_instance is not None
        assert stored_instance.status is InstanceStatus.ERROR

    ready_instance, owner, template, image = await create_ready_context(client, session_maker)
    workspace_response = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(ready_instance, owner, template, image, name="guarded"),
    )
    workspace_id = UUID(workspace_response.json()["id"])
    tasks.create_workspace.on_failure(
        RuntimeError("workspace worker failed"),
        "workspace-task-id",
        (str(workspace_id),),
        {},
        None,
    )
    async with session_maker() as session:
        stored_workspace = await session.get(Workspace, workspace_id)
        assert stored_workspace is not None
        assert stored_workspace.status.value == "error"


async def test_workspace_create_update_delete_duplicate_and_error(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the workspace create update delete duplicate and error scenario."""

    instance, member, template, image = await create_ready_context(client, session_maker)
    response = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(instance, member, template, image),
    )
    workspace_id = UUID(response.json()["id"])

    created = tasks._workspace_lifecycle(
        workspace_id,
        expected_action="creating",
        delete_on_success=False,
        session_factory=sync_session_maker,
    )
    duplicate = tasks._workspace_lifecycle(
        workspace_id,
        expected_action="creating",
        delete_on_success=False,
        session_factory=sync_session_maker,
    )
    assert created == {"status": "success"}
    assert duplicate == {"status": "noop"}

    updated_response = await client.put(
        f"/api/v1/workspaces/{workspace_id}",
        json={
            "name": "renamed",
            "image_id": image["id"],
            "modules": [],
            "cpu": 3,
            "ram": 9,
        },
    )
    assert updated_response.status_code == 202
    updated = tasks._workspace_lifecycle(
        workspace_id,
        expected_action="updating",
        delete_on_success=False,
        session_factory=sync_session_maker,
    )
    assert updated == {"status": "success"}

    deleted_response = await client.delete(f"/api/v1/workspaces/{workspace_id}")
    assert deleted_response.status_code == 202
    deleted = tasks._workspace_lifecycle(
        workspace_id,
        expected_action="deleting",
        delete_on_success=True,
        session_factory=sync_session_maker,
    )
    assert deleted == {"status": "deleted"}
    assert tasks._workspace_lifecycle(
        workspace_id,
        expected_action="deleting",
        delete_on_success=True,
        session_factory=sync_session_maker,
    ) == {"status": "noop"}

    failing_response = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(instance, member, template, image, name="failing"),
    )
    failing_id = UUID(failing_response.json()["id"])

    def failing_placeholder() -> None:
        """Simulate the expected failing placeholder behavior."""

        raise RuntimeError("workspace failed")

    monkeypatch.setattr(task_common, "placeholder", failing_placeholder)
    with pytest.raises(RuntimeError, match="workspace failed"):
        tasks._workspace_lifecycle(
            failing_id,
            expected_action="creating",
            delete_on_success=False,
            session_factory=sync_session_maker,
        )
    async with session_maker() as session:
        stored = await session.get(Workspace, failing_id)
        assert stored is not None
        assert stored.status.value == "error"


async def test_update_instance_finalizes_and_coalesces_members(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
) -> None:
    """Verify the update instance finalizes and coalesces members scenario."""

    application = await create_application(client)
    instance = await create_instance(client, application["id"])
    instance_id = UUID(str(instance["id"]))
    await set_instance_status(session_maker, instance_id)
    tasks.update_instance.delay.reset_mock()
    first_response = await client.post(
        f"/api/v1/instances/{instance_id}/members",
        json={"username": "first", "role": "user"},
    )
    second_response = await client.post(
        f"/api/v1/instances/{instance_id}/members",
        json={"username": "second", "role": "user"},
    )
    first = first_response.json()
    second = second_response.json()
    tasks.update_instance.delay.assert_called_once_with(str(instance_id))

    reconciled_members: list[tuple[tuple[str, str], ...]] = []

    def record_reconcile(
        reconciled_instance_id: UUID,
        attached_name: str | None,
        members: tuple[tuple[str, str], ...],
        region: str,
        environment: str,
    ) -> str:
        """Record the reconcile calls made by this scenario."""

        reconciled_members.append(members)
        assert region == "emea"
        assert environment == "development"
        return attached_name or f"coder-{reconciled_instance_id.hex}"

    first_pass = tasks._update_instance(
        instance_id,
        sync_session_maker,
        record_reconcile,
    )
    assert first_pass == {"status": "success"}
    assert reconciled_members[-1] == (("first", "user"), ("second", "user"))
    async with session_maker() as session:
        stored_members = list(
            await session.scalars(
                select(Member).where(Member.instance_id == instance_id).order_by(Member.username)
            )
        )
        assert [member.status for member in stored_members] == [
            MemberStatus.SUCCESS,
            MemberStatus.SUCCESS,
        ]

    updated = await client.put(
        f"/api/v1/instances/{instance_id}/members/{first['id']}",
        json={"role": "admin"},
    )
    removed = await client.delete(f"/api/v1/instances/{instance_id}/members/{second['id']}")
    assert updated.status_code == 202
    assert removed.status_code == 202
    assert tasks._update_instance(
        instance_id,
        sync_session_maker,
        record_reconcile,
    ) == {"status": "success"}
    assert reconciled_members[-1] == (("first", "admin"),)
    async with session_maker() as session:
        first_member = await session.get(Member, UUID(str(first["id"])))
        second_member = await session.get(Member, UUID(str(second["id"])))
        assert first_member is not None
        assert first_member.status is MemberStatus.SUCCESS
        assert second_member is None

    third_response = await client.post(
        f"/api/v1/instances/{instance_id}/members",
        json={"username": "third", "role": "user"},
    )
    third = third_response.json()
    tasks.update_instance.delay.reset_mock()

    def add_member_during_pass(
        _instance_id: UUID,
        attached_name: str | None,
        _members: tuple[tuple[str, str], ...],
        _region: str,
        _environment: str,
    ) -> str:
        """Add member during pass during the test scenario."""

        with sync_session_maker() as session:
            session.add(Member(instance_id=instance_id, username="late", role="user"))
            session.commit()
        return attached_name or f"coder-{instance_id.hex}"

    coalesced = tasks._update_instance(
        instance_id,
        sync_session_maker,
        add_member_during_pass,
    )
    assert coalesced == {"status": "pending"}
    tasks.update_instance.delay.assert_called_once_with(str(instance_id))
    async with session_maker() as session:
        third_member = await session.get(Member, UUID(str(third["id"])))
        late_member = await session.scalar(select(Member).where(Member.username == "late"))
        assert third_member is not None
        assert third_member.status is MemberStatus.SUCCESS
        assert late_member is not None
        assert late_member.status is MemberStatus.PENDING

    assert tasks._update_instance(
        instance_id,
        sync_session_maker,
        record_reconcile,
    ) == {"status": "success"}
    assert reconciled_members[-1] == (
        ("first", "admin"),
        ("late", "user"),
        ("third", "user"),
    )


async def test_update_instance_error_and_instance_delete_cascade(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the update instance error and instance delete cascade scenario."""

    application = await create_application(client, suffix="failed")
    instance = await create_instance(client, application["id"])
    instance_id = UUID(str(instance["id"]))
    await set_instance_status(session_maker, instance_id)
    member_response = await client.post(
        f"/api/v1/instances/{instance_id}/members",
        json={"username": "failing", "role": "user"},
    )
    member = member_response.json()

    def failing_reconcile(
        _instance_id: UUID,
        _attached_name: str | None,
        _members: tuple[tuple[str, str], ...],
        _region: str,
        _environment: str,
    ) -> str:
        """Simulate the expected failing reconcile behavior."""

        raise RuntimeError("reconciliation failed")

    with pytest.raises(RuntimeError, match="reconciliation failed"):
        tasks._update_instance(instance_id, sync_session_maker, failing_reconcile)
    async with session_maker() as session:
        stored_instance = await session.get(Instance, instance_id)
        stored_member = await session.get(Member, UUID(str(member["id"])))
        assert stored_instance is not None
        assert stored_instance.status is InstanceStatus.ERROR
        assert stored_member is not None
        assert stored_member.status is MemberStatus.ERROR

    managed_instance, owner, template, image = await create_ready_context(client, session_maker)
    managed_id = UUID(str(managed_instance["id"]))
    workspace_response = await client.post(
        "/api/v1/workspaces",
        json=workspace_payload(managed_instance, owner, template, image),
    )
    assert workspace_response.status_code == 201
    deletion = await client.delete(f"/api/v1/instances/{managed_id}")
    assert deletion.status_code == 202

    def successful_placeholder() -> None:
        """Simulate a successful placeholder operation."""

        return

    monkeypatch.setattr(task_common, "placeholder", successful_placeholder)
    result = tasks._delete_instance(managed_id, sync_session_maker)
    assert result == {"status": "deleted"}
    async with session_maker() as session:
        assert await session.get(Instance, managed_id) is None
        member_count = await session.scalar(
            select(func.count()).select_from(Member).where(Member.instance_id == managed_id)
        )
        workspace_count = await session.scalar(
            select(func.count()).select_from(Workspace).where(Workspace.instance_id == managed_id)
        )
        allocation_count = await session.scalar(
            select(func.count())
            .select_from(DatabaseAllocation)
            .where(DatabaseAllocation.instance_id == managed_id)
        )
        assert member_count == 0
        assert workspace_count == 0
        assert allocation_count == 0


async def test_missing_update_instance_is_a_noop(
    sync_session_maker: sessionmaker[Session],
) -> None:
    """Verify the missing update instance is a noop scenario."""

    assert tasks._update_instance(uuid4(), sync_session_maker) == {"status": "noop"}


async def test_force_sync_accepts_idle_instances_and_rejects_busy_or_missing(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the force sync accepts idle instances and rejects busy or missing scenario."""

    application = await create_application(client, suffix="force-sync")
    instance = await create_instance(client, application["id"])
    instance_id = UUID(str(instance["id"]))

    busy = await client.post(f"/api/v1/instances/{instance_id}/sync")
    forced_creation = await client.post(f"/api/v1/instances/{instance_id}/sync?force=true")
    assert busy.status_code == 409
    assert forced_creation.status_code == 409
    assert busy.json() == {"detail": "Instance has an action in progress"}

    await set_instance_status(session_maker, instance_id)
    tasks.update_instance.delay.reset_mock()
    accepted = await client.post(f"/api/v1/instances/{instance_id}/sync")
    repeated = await client.post(f"/api/v1/instances/{instance_id}/sync")
    forced = await client.post(f"/api/v1/instances/{instance_id}/sync?force=true")
    missing = await client.post(f"/api/v1/instances/{uuid4()}/sync")

    assert accepted.status_code == 202
    assert accepted.json()["action"] == "updating"
    assert accepted.json()["status"] == "pending"
    assert repeated.status_code == 409
    assert forced.status_code == 202
    assert forced.json()["action"] == "updating"
    assert forced.json()["status"] == "pending"
    assert missing.status_code == 404
    assert tasks.update_instance.delay.call_args_list == [
        ((str(instance_id),), {}),
        ((str(instance_id),), {"force": True}),
    ]


async def test_forced_sync_finalizes_a_running_transition(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
) -> None:
    """Let a successful forced reconciliation finalize the active transition."""

    application = await create_application(client, suffix="forced-running-sync")
    instance = await create_instance(client, application["id"])
    instance_id = UUID(str(instance["id"]))
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        stored.action = "updating"
        stored.status = InstanceStatus.RUNNING
        running_member = Member(
            instance_id=instance_id,
            username="running-member",
            role="user",
            action="updating",
            status=MemberStatus.RUNNING,
        )
        deleting_member = Member(
            instance_id=instance_id,
            username="deleting-member",
            role="user",
            action="deleting",
            status=MemberStatus.RUNNING,
        )
        session.add_all([running_member, deleting_member])
        await session.commit()
        running_member_id = running_member.id
        deleting_member_id = deleting_member.id

    tasks.update_instance.delay.reset_mock()
    accepted = await client.post(f"/api/v1/instances/{instance_id}/sync?force=true")
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "running"
    tasks.update_instance.delay.assert_called_once_with(str(instance_id), force=True)

    reconciled = False

    def record_reconcile(
        observed_instance_id: UUID,
        _attached_name: str | None,
        _members: tuple[tuple[str, str], ...],
        _region: str,
        _environment: str,
    ) -> str:
        """Record the forced remote reconciliation."""

        nonlocal reconciled
        reconciled = True
        assert observed_instance_id == instance_id
        assert _members == (("running-member", "user"),)
        return f"coder-{instance_id.hex}"

    result = tasks._update_instance(
        instance_id,
        sync_session_maker,
        record_reconcile,
        force=True,
    )
    assert result == {"status": "success"}
    assert reconciled is True
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        stored_running_member = await session.get(Member, running_member_id)
        assert stored is not None
        assert stored.action == "updating"
        assert stored.status is InstanceStatus.SUCCESS
        assert stored_running_member is not None
        assert stored_running_member.status is MemberStatus.SUCCESS
        assert await session.get(Member, deleting_member_id) is None


async def test_forced_sync_failure_finalizes_a_running_transition_as_error(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
) -> None:
    """Let a failed forced reconciliation finalize the active transition."""

    application = await create_application(client, suffix="forced-running-error")
    instance = await create_instance(client, application["id"])
    instance_id = UUID(str(instance["id"]))
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        stored.action = "updating"
        stored.status = InstanceStatus.RUNNING
        running_member = Member(
            instance_id=instance_id,
            username="failed-member",
            role="user",
            action="updating",
            status=MemberStatus.RUNNING,
        )
        session.add(running_member)
        await session.commit()
        running_member_id = running_member.id

    def fail_reconcile(
        _instance_id: UUID,
        _attached_name: str | None,
        _members: tuple[tuple[str, str], ...],
        _region: str,
        _environment: str,
    ) -> str:
        """Fail the forced reconciliation."""

        raise RuntimeError("forced reconciliation failed")

    with pytest.raises(RuntimeError, match="forced reconciliation failed"):
        tasks._update_instance(
            instance_id,
            sync_session_maker,
            fail_reconcile,
            force=True,
        )

    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        stored_member = await session.get(Member, running_member_id)
        assert stored is not None
        assert stored.status is InstanceStatus.ERROR
        assert stored_member is not None
        assert stored_member.status is MemberStatus.ERROR


async def test_force_sync_retries_failed_member_deletion(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
) -> None:
    """Verify the force sync retries failed member deletion scenario."""

    application = await create_application(client, suffix="force-sync-error")
    instance = await create_instance(client, application["id"])
    instance_id = UUID(str(instance["id"]))
    await set_instance_status(session_maker, instance_id)
    member_response = await client.post(
        f"/api/v1/instances/{instance_id}/members",
        json={"username": "failed-admin", "role": "admin"},
    )
    member_id = UUID(member_response.json()["id"])
    async with session_maker() as session:
        stored_instance = await session.get(Instance, instance_id)
        stored_member = await session.get(Member, member_id)
        assert stored_instance is not None
        assert stored_member is not None
        stored_instance.action = "updating"
        stored_instance.status = InstanceStatus.ERROR
        stored_member.action = "deleting"
        stored_member.status = MemberStatus.ERROR
        await session.commit()

    accepted = await client.post(f"/api/v1/instances/{instance_id}/sync")
    assert accepted.status_code == 202
    async with session_maker() as session:
        reset_member = await session.get(Member, member_id)
        assert reset_member is not None
        assert reset_member.status is MemberStatus.PENDING

    observed_members: tuple[tuple[str, str], ...] | None = None

    def capture_reconcile(
        _instance_id: UUID,
        _attached_name: str | None,
        members: tuple[tuple[str, str], ...],
        _region: str,
        _environment: str,
    ) -> str:
        """Record the reconcile calls made by this scenario."""

        nonlocal observed_members
        observed_members = members
        return f"coder-{instance_id.hex}"

    assert tasks._update_instance(
        instance_id,
        sync_session_maker,
        capture_reconcile,
    ) == {"status": "success"}
    assert observed_members == ()
    async with session_maker() as session:
        stored_instance = await session.get(Instance, instance_id)
        assert stored_instance is not None
        assert stored_instance.status is InstanceStatus.SUCCESS
        assert stored_instance.argocd_application_name == f"coder-{instance_id.hex}"
        assert await session.get(Member, member_id) is None


async def test_public_tasks_run_sequentially_inside_an_active_event_loop(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    sync_session_maker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the public tasks run sequentially inside an active event loop scenario."""

    first_application = await create_application(client, suffix="sequential-first")
    second_application = await create_application(client, suffix="sequential-second")
    first = await create_instance(client, first_application["id"])
    second = await create_instance(client, second_application["id"])
    monkeypatch.setattr(
        worker_database,
        "get_worker_session_maker",
        lambda: sync_session_maker,
    )
    monkeypatch.setattr(argocd, "reconcile_instance_application", successful_reconcile)

    assert tasks.create_instance.run(str(first["id"])) == {"status": "success"}
    assert tasks.create_instance.run(str(second["id"])) == {"status": "success"}

    async with session_maker() as session:
        first_stored = await session.get(Instance, UUID(str(first["id"])))
        second_stored = await session.get(Instance, UUID(str(second["id"])))
        assert first_stored is not None
        assert second_stored is not None
        assert first_stored.status is InstanceStatus.SUCCESS
        assert second_stored.status is InstanceStatus.SUCCESS
