"""Instance member API and state transition tests."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, Response
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coder_manager.api.routes import members as member_routes
from coder_manager.models import Instance, InstanceStatus, Member, MemberRole, MemberStatus
from coder_manager.repositories import (
    InstanceRepository,
    InvalidMemberActionError,
    MemberActionConflictError,
    MemberAlreadyExistsError,
    MemberInstanceBusyError,
    MemberInstanceNotFoundError,
    MemberNotFoundError,
    MemberRepository,
)
from coder_manager.schemas import MemberCreate, MemberRoleUpdate


async def create_application(client: AsyncClient, suffix: str = "1") -> dict[str, object]:
    """Create and whitelist one application."""

    response = await client.post(
        "/api/v1/applications",
        json={"external_id": f"member-app-{suffix}", "name": f"Member App {suffix}"},
    )
    assert response.status_code == 201
    application = response.json()
    enabled = await client.post(f"/api/v1/applications/{application['id']}/whitelist")
    assert enabled.status_code == 204
    return application


async def create_instance(client: AsyncClient, application_id: object) -> dict[str, object]:
    """Create one pending instance."""

    response = await client.post(
        "/api/v1/instances",
        json={
            "application_id": application_id,
            "region": "emea",
            "environment": "development",
        },
    )
    assert response.status_code == 201
    return response.json()


async def set_instance_status(
    session_maker: async_sessionmaker[AsyncSession],
    instance_id: str,
    *,
    expected_action: str = "creating",
    action: str = "creating",
    status: InstanceStatus = InstanceStatus.SUCCESS,
) -> None:
    """Transition an instance through its guarded internal repository API."""

    async with session_maker() as session:
        await InstanceRepository(session).update_action(
            UUID(instance_id),
            expected_action=expected_action,
            action=action,
            status=status,
        )


async def create_ready_instance(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    *,
    suffix: str = "1",
) -> dict[str, object]:
    """Create an instance whose initial action has succeeded."""

    application = await create_application(client, suffix)
    instance = await create_instance(client, application["id"])
    await set_instance_status(session_maker, str(instance["id"]))
    return instance


async def create_member(
    client: AsyncClient,
    instance_id: object,
    *,
    username: str = "alice",
    role: str = "user",
) -> dict[str, object]:
    """Add and return one member through the API."""

    response = await client.post(
        f"/api/v1/instances/{instance_id}/members",
        json={"username": username, "role": role},
    )
    assert response.status_code == 201
    return response.json()


async def set_member_status(
    session_maker: async_sessionmaker[AsyncSession],
    member_id: str,
    *,
    expected_action: str = "creating",
    action: str = "creating",
    status: MemberStatus = MemberStatus.SUCCESS,
) -> dict[str, object]:
    """Transition a member and return selected updated state."""

    async with session_maker() as session:
        member = await MemberRepository(session).update_action(
            UUID(member_id),
            expected_action=expected_action,
            action=action,
            status=status,
        )
        instance = await session.get(Instance, member.instance_id)
        assert instance is not None
        if instance.action == "updating":
            instance.status = (
                InstanceStatus.SUCCESS if status is MemberStatus.SUCCESS else InstanceStatus.ERROR
            )
            await session.commit()
        return {
            "action": member.action,
            "status": member.status,
            "updated_at": member.updated_at,
        }


async def test_member_crud_normalization_and_timestamps(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the member crud normalization and timestamps scenario."""

    instance = await create_ready_instance(client, session_maker)
    created = await create_member(
        client,
        instance["id"],
        username="  Alice.Example  ",
    )

    assert set(created) == {
        "id",
        "instance_id",
        "username",
        "role",
        "action",
        "status",
        "created_at",
        "updated_at",
    }
    assert created["username"] == "alice.example"
    assert created["role"] == "user"
    assert created["action"] == "creating"
    assert created["status"] == "pending"
    assert datetime.fromisoformat(str(created["created_at"]))
    assert datetime.fromisoformat(str(created["updated_at"]))

    fetched = await client.get(f"/api/v1/instances/{instance['id']}/members/{created['id']}")
    listed = await client.get(f"/api/v1/instances/{instance['id']}/members")
    assert fetched.status_code == 200
    assert fetched.json() == created
    assert listed.status_code == 200
    assert listed.json()["items"] == [created]

    duplicate = await client.post(
        f"/api/v1/instances/{instance['id']}/members",
        json={"username": " ALICE.EXAMPLE ", "role": "admin"},
    )
    assert duplicate.status_code == 409

    premature = await client.put(
        f"/api/v1/instances/{instance['id']}/members/{created['id']}",
        json={"role": "admin"},
    )
    assert premature.status_code == 409

    await set_member_status(session_maker, str(created["id"]))
    successful = await client.get(f"/api/v1/instances/{instance['id']}/members/{created['id']}")
    unchanged_timestamp = successful.json()["updated_at"]
    no_op = await client.put(
        f"/api/v1/instances/{instance['id']}/members/{created['id']}",
        json={"role": "user"},
    )
    assert no_op.status_code == 200
    assert no_op.json()["updated_at"] == unchanged_timestamp

    async with session_maker() as session:
        member = await session.get(Member, UUID(str(created["id"])))
        assert member is not None
        member.updated_at = datetime.now(UTC) - timedelta(days=1)
        await session.commit()

    updated = await client.put(
        f"/api/v1/instances/{instance['id']}/members/{created['id']}",
        json={"role": "admin"},
    )
    assert updated.status_code == 202
    assert updated.json()["role"] == "admin"
    assert updated.json()["action"] == "updating"
    assert updated.json()["status"] == "pending"
    updated_at = datetime.fromisoformat(updated.json()["updated_at"]).replace(tzinfo=UTC)
    assert updated_at > datetime.now(UTC) - timedelta(hours=1)

    await set_member_status(
        session_maker,
        str(created["id"]),
        expected_action="updating",
        action="updating",
    )
    deleted = await client.delete(f"/api/v1/instances/{instance['id']}/members/{created['id']}")
    assert deleted.status_code == 202
    assert deleted.json()["action"] == "deleting"
    assert deleted.json()["status"] == "pending"


async def test_instance_busy_blocks_mutations_but_not_reads(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the instance busy blocks mutations but not reads scenario."""

    application = await create_application(client)
    pending_instance = await create_instance(client, application["id"])

    blocked_create = await client.post(
        f"/api/v1/instances/{pending_instance['id']}/members",
        json={"username": "alice", "role": "user"},
    )
    readable = await client.get(f"/api/v1/instances/{pending_instance['id']}/members")
    assert blocked_create.status_code == 409
    assert blocked_create.json() == {"detail": "Instance has an action in progress"}
    assert readable.status_code == 200
    assert readable.json()["total"] == 0

    await set_instance_status(session_maker, str(pending_instance["id"]))
    member = await create_member(client, pending_instance["id"])
    await set_member_status(session_maker, str(member["id"]))
    await set_instance_status(
        session_maker,
        str(pending_instance["id"]),
        expected_action="updating",
        action="synchronizing",
        status=InstanceStatus.RUNNING,
    )

    blocked_update = await client.put(
        f"/api/v1/instances/{pending_instance['id']}/members/{member['id']}",
        json={"role": "admin"},
    )
    blocked_delete = await client.delete(
        f"/api/v1/instances/{pending_instance['id']}/members/{member['id']}"
    )
    readable_member = await client.get(
        f"/api/v1/instances/{pending_instance['id']}/members/{member['id']}"
    )
    assert blocked_update.status_code == 409
    assert blocked_delete.status_code == 409
    assert readable_member.status_code == 200


async def test_instance_error_allows_member_creation(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the instance error allows member creation scenario."""

    application = await create_application(client)
    instance = await create_instance(client, application["id"])
    await set_instance_status(
        session_maker,
        str(instance["id"]),
        status=InstanceStatus.ERROR,
    )

    created = await create_member(client, instance["id"])

    assert created["username"] == "alice"


async def test_member_error_blocks_role_update_and_deletion(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the member error blocks role update and deletion scenario."""

    instance = await create_ready_instance(client, session_maker)
    member = await create_member(client, instance["id"])
    await set_member_status(
        session_maker,
        str(member["id"]),
        status=MemberStatus.ERROR,
    )

    update = await client.put(
        f"/api/v1/instances/{instance['id']}/members/{member['id']}",
        json={"role": "admin"},
    )
    deletion = await client.delete(f"/api/v1/instances/{instance['id']}/members/{member['id']}")

    assert update.status_code == 409
    assert deletion.status_code == 409


async def test_members_are_paginated_and_isolated_by_instance(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the members are paginated and isolated by instance scenario."""

    first = await create_ready_instance(client, session_maker, suffix="1")
    second = await create_ready_instance(client, session_maker, suffix="2")
    first_member_ids = []
    for username in ("charlie", "alice", "bob"):
        member = await create_member(client, first["id"], username=username)
        first_member_ids.append(member["id"])
    second_member = await create_member(client, second["id"], username="alice")

    page = await client.get(
        f"/api/v1/instances/{first['id']}/members",
        params={"page": 2, "page_size": 2},
    )
    cross_instance = await client.get(
        f"/api/v1/instances/{second['id']}/members/{first_member_ids[0]}"
    )

    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert page.json()["pages"] == 2
    assert [item["username"] for item in page.json()["items"]] == ["charlie"]
    assert cross_instance.status_code == 404
    assert second_member["username"] == "alice"


async def test_missing_resources_and_invalid_payloads(client: AsyncClient) -> None:
    """Verify the missing resources and invalid payloads scenario."""

    missing_instance_id = uuid4()
    missing_member_id = uuid4()
    missing_list = await client.get(f"/api/v1/instances/{missing_instance_id}/members")
    missing_create = await client.post(
        f"/api/v1/instances/{missing_instance_id}/members",
        json={"username": "alice", "role": "user"},
    )
    missing_get = await client.get(
        f"/api/v1/instances/{missing_instance_id}/members/{missing_member_id}"
    )
    missing_update = await client.put(
        f"/api/v1/instances/{missing_instance_id}/members/{missing_member_id}",
        json={"role": "admin"},
    )
    missing_delete = await client.delete(
        f"/api/v1/instances/{missing_instance_id}/members/{missing_member_id}"
    )

    assert missing_list.status_code == 404
    assert missing_create.status_code == 404
    assert missing_get.status_code == 404
    assert missing_update.status_code == 404
    assert missing_delete.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"username": "   ", "role": "user"},
        {"username": "a" * 256, "role": "user"},
        {"username": "alice,bob", "role": "user"},
        {"username": "alice", "role": "owner"},
        {"username": "alice", "role": "user", "extra": True},
    ],
)
async def test_member_create_validation(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    payload: dict[str, object],
) -> None:
    """Verify the member create validation scenario."""

    instance = await create_ready_instance(client, session_maker)

    response = await client.post(
        f"/api/v1/instances/{instance['id']}/members",
        json=payload,
    )

    assert response.status_code == 422


async def test_member_update_validation_and_missing_member(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the member update validation and missing member scenario."""

    instance = await create_ready_instance(client, session_maker)
    missing_id = uuid4()

    invalid_role = await client.put(
        f"/api/v1/instances/{instance['id']}/members/{missing_id}",
        json={"role": "owner"},
    )
    invalid_page = await client.get(
        f"/api/v1/instances/{instance['id']}/members",
        params={"page": 0, "page_size": 101},
    )
    missing_update = await client.put(
        f"/api/v1/instances/{instance['id']}/members/{missing_id}",
        json={"role": "admin"},
    )
    missing_delete = await client.delete(f"/api/v1/instances/{instance['id']}/members/{missing_id}")

    assert invalid_role.status_code == 422
    assert invalid_page.status_code == 422
    assert missing_update.status_code == 404
    assert missing_delete.status_code == 404


async def test_internal_member_actions_validate_input_and_staleness(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the internal member actions validate input and staleness scenario."""

    instance = await create_ready_instance(client, session_maker)
    member = await create_member(client, instance["id"])
    member_id = UUID(str(member["id"]))

    async with session_maker() as session:
        repository = MemberRepository(session)
        updated = await repository.update_action(
            member_id,
            expected_action="creating",
            action="provisioning-access",
            status=MemberStatus.RUNNING,
        )
        assert updated.action == "provisioning-access"

        with pytest.raises(MemberActionConflictError):
            await repository.update_action(
                member_id,
                expected_action="creating",
                action="creating",
                status=MemberStatus.ERROR,
            )
        with pytest.raises(InvalidMemberActionError):
            await repository.update_action(
                member_id,
                expected_action="provisioning-access",
                action="   ",
                status=MemberStatus.SUCCESS,
            )
        with pytest.raises(InvalidMemberActionError):
            await repository.update_action(
                member_id,
                expected_action="provisioning-access",
                action="a" * 256,
                status=MemberStatus.SUCCESS,
            )
        with pytest.raises(MemberNotFoundError):
            await repository.update_action(
                uuid4(),
                expected_action="creating",
                action="creating",
                status=MemberStatus.SUCCESS,
            )


async def test_instance_updated_at_changes_on_real_transition(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the instance updated at changes on real transition scenario."""

    application = await create_application(client)
    instance = await create_instance(client, application["id"])
    instance_id = UUID(str(instance["id"]))
    old_timestamp = datetime.now(UTC) - timedelta(days=1)
    async with session_maker() as session:
        stored = await session.get(Instance, instance_id)
        assert stored is not None
        stored.updated_at = old_timestamp
        await session.commit()

    await set_instance_status(session_maker, str(instance["id"]))
    fetched = await client.get(f"/api/v1/instances/{instance['id']}")

    assert fetched.status_code == 200
    updated_at = datetime.fromisoformat(fetched.json()["updated_at"]).replace(tzinfo=UTC)
    assert updated_at > datetime.now(UTC) - timedelta(hours=1)


def member_record() -> SimpleNamespace:
    """Build an ORM-shaped member for isolated route tests."""

    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid4(),
        instance_id=uuid4(),
        username="alice",
        role=MemberRole.USER,
        action="creating",
        status=MemberStatus.SUCCESS,
        created_at=now,
        updated_at=now,
    )


async def test_member_route_success_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the member route success mapping scenario."""

    record = member_record()

    class SuccessfulRepository:
        """Provide the successful repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def list(
            self,
            _instance_id: UUID,
            *,
            page: int,
            page_size: int,
        ) -> tuple[list[SimpleNamespace], int]:
            """Simulate the repository list operation."""

            assert (page, page_size) == (1, 20)
            return [record], 1

        async def get(self, _instance_id: UUID, _member_id: UUID) -> SimpleNamespace:
            """Simulate the repository get operation."""

            return record

        async def create(
            self,
            _instance_id: UUID,
            _payload: MemberCreate,
        ) -> SimpleNamespace:
            """Simulate the repository create operation."""

            return record

        async def update_role(
            self,
            _instance_id: UUID,
            _member_id: UUID,
            _payload: MemberRoleUpdate,
        ) -> tuple[SimpleNamespace, bool]:
            """Provide the update role helper used by this test scenario."""

            return record, True

        async def request_deletion(
            self,
            _instance_id: UUID,
            _member_id: UUID,
        ) -> SimpleNamespace:
            """Simulate the repository request deletion operation."""

            return record

    monkeypatch.setattr(member_routes, "MemberRepository", SuccessfulRepository)
    create_payload = MemberCreate(username="alice", role=MemberRole.USER)
    update_payload = MemberRoleUpdate(role=MemberRole.ADMIN)
    response = Response()

    page = await member_routes.list_members(record.instance_id, None, 1, 20)
    created = await member_routes.create_member(record.instance_id, create_payload, None)
    fetched = await member_routes.get_member(record.instance_id, record.id, None)
    updated = await member_routes.update_member_role(
        record.instance_id,
        record.id,
        update_payload,
        None,
        response,
    )
    deleted = await member_routes.delete_member(record.instance_id, record.id, None)

    assert page.total == 1
    assert created.id == fetched.id == updated.id == deleted.id == record.id
    assert response.status_code == 202


async def test_member_route_no_op_keeps_default_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the member route no op keeps default status scenario."""

    record = member_record()

    class NoOpRepository:
        """Provide the no op repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def update_role(
            self,
            _instance_id: UUID,
            _member_id: UUID,
            _payload: MemberRoleUpdate,
        ) -> tuple[SimpleNamespace, bool]:
            """Provide the update role helper used by this test scenario."""

            return record, False

    monkeypatch.setattr(member_routes, "MemberRepository", NoOpRepository)
    response = Response(status_code=200)

    result = await member_routes.update_member_role(
        record.instance_id,
        record.id,
        MemberRoleUpdate(role=MemberRole.USER),
        None,
        response,
    )

    assert result.id == record.id
    assert response.status_code == 200


async def test_member_route_not_found_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the member route not found mapping scenario."""

    class MissingRepository:
        """Provide the missing repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def list(self, *_args: object, **_kwargs: object) -> None:
            """Simulate the repository list operation."""

            raise MemberInstanceNotFoundError

        async def get(self, *_args: object) -> None:
            """Simulate the repository get operation."""

            return

    monkeypatch.setattr(member_routes, "MemberRepository", MissingRepository)

    with pytest.raises(HTTPException) as missing_list:
        await member_routes.list_members(uuid4(), None, 1, 20)
    with pytest.raises(HTTPException) as missing_member:
        await member_routes.get_member(uuid4(), uuid4(), None)

    assert missing_list.value.status_code == 404
    assert missing_member.value.status_code == 404


@pytest.mark.parametrize(
    ("repository_error", "expected_status"),
    [
        (MemberInstanceNotFoundError, 404),
        (MemberInstanceBusyError, 409),
        (MemberAlreadyExistsError, 409),
    ],
)
async def test_create_member_route_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
    repository_error: type[Exception],
    expected_status: int,
) -> None:
    """Verify the create member route error mapping scenario."""

    class FailingRepository:
        """Provide the failing repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def create(self, *_args: object) -> None:
            """Simulate the repository create operation."""

            raise repository_error

    monkeypatch.setattr(member_routes, "MemberRepository", FailingRepository)

    with pytest.raises(HTTPException) as caught:
        await member_routes.create_member(
            uuid4(),
            MemberCreate(username="alice", role=MemberRole.USER),
            None,
        )

    assert caught.value.status_code == expected_status


@pytest.mark.parametrize(
    "repository_error",
    [
        MemberInstanceNotFoundError,
        MemberInstanceBusyError,
        MemberNotFoundError,
        MemberActionConflictError,
    ],
)
async def test_update_member_route_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
    repository_error: type[Exception],
) -> None:
    """Verify the update member route error mapping scenario."""

    class FailingRepository:
        """Provide the failing repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def update_role(self, *_args: object) -> None:
            """Provide the update role helper used by this test scenario."""

            raise repository_error

    monkeypatch.setattr(member_routes, "MemberRepository", FailingRepository)

    with pytest.raises(HTTPException) as caught:
        await member_routes.update_member_role(
            uuid4(),
            uuid4(),
            MemberRoleUpdate(role=MemberRole.ADMIN),
            None,
            Response(),
        )

    assert caught.value.status_code in {404, 409}


@pytest.mark.parametrize(
    "repository_error",
    [
        MemberInstanceNotFoundError,
        MemberInstanceBusyError,
        MemberNotFoundError,
        MemberActionConflictError,
    ],
)
async def test_delete_member_route_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
    repository_error: type[Exception],
) -> None:
    """Verify the delete member route error mapping scenario."""

    class FailingRepository:
        """Provide the failing repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def request_deletion(self, *_args: object) -> None:
            """Simulate the repository request deletion operation."""

            raise repository_error

    monkeypatch.setattr(member_routes, "MemberRepository", FailingRepository)

    with pytest.raises(HTTPException) as caught:
        await member_routes.delete_member(uuid4(), uuid4(), None)

    assert caught.value.status_code in {404, 409}


async def test_member_repository_crud_transitions(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the member repository crud transitions scenario."""

    instance = await create_ready_instance(client, session_maker)
    instance_id = UUID(str(instance["id"]))
    payload = MemberCreate(username="alice", role=MemberRole.USER)

    async with session_maker() as session:
        repository = MemberRepository(session)
        member = await repository.create(instance_id, payload)
        member_id = member.id
        members, total = await repository.list(instance_id, page=1, page_size=20)
        assert total == 1
        assert members[0].id == member_id
        assert await repository.get(instance_id, member_id) is not None

        with pytest.raises(MemberAlreadyExistsError):
            await repository.create(instance_id, payload)
        with pytest.raises(MemberActionConflictError):
            await repository.update_role(
                instance_id,
                member_id,
                MemberRoleUpdate(role=MemberRole.ADMIN),
            )

        await repository.update_action(
            member_id,
            expected_action="creating",
            action="creating",
            status=MemberStatus.SUCCESS,
        )
        unchanged, changed = await repository.update_role(
            instance_id,
            member_id,
            MemberRoleUpdate(role=MemberRole.USER),
        )
        assert unchanged.role is MemberRole.USER
        assert changed is False

        updated, changed = await repository.update_role(
            instance_id,
            member_id,
            MemberRoleUpdate(role=MemberRole.ADMIN),
        )
        assert updated.action == "updating"
        assert changed is True
        await repository.update_action(
            member.id,
            expected_action="updating",
            action="updating",
            status=MemberStatus.SUCCESS,
        )
        deleted = await repository.request_deletion(instance_id, member_id)
        assert deleted.action == "deleting"


async def test_member_repository_rejects_missing_and_busy_parents(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the member repository rejects missing and busy parents scenario."""

    missing_instance_id = uuid4()
    async with session_maker() as session:
        repository = MemberRepository(session)
        with pytest.raises(MemberInstanceNotFoundError):
            await repository.list(missing_instance_id, page=1, page_size=20)
        with pytest.raises(MemberInstanceNotFoundError):
            await repository.create(
                missing_instance_id,
                MemberCreate(username="alice", role=MemberRole.USER),
            )

    application = await create_application(client)
    instance = await create_instance(client, application["id"])
    async with session_maker() as session:
        repository = MemberRepository(session)
        with pytest.raises(MemberInstanceBusyError):
            await repository.create(
                UUID(str(instance["id"])),
                MemberCreate(username="alice", role=MemberRole.USER),
            )


async def test_member_repository_rejects_missing_members(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the member repository rejects missing members scenario."""

    instance = await create_ready_instance(client, session_maker)
    instance_id = UUID(str(instance["id"]))
    missing_member_id = uuid4()

    async with session_maker() as session:
        repository = MemberRepository(session)
        with pytest.raises(MemberNotFoundError):
            await repository.update_role(
                instance_id,
                missing_member_id,
                MemberRoleUpdate(role=MemberRole.ADMIN),
            )
        with pytest.raises(MemberNotFoundError):
            await repository.request_deletion(instance_id, missing_member_id)
