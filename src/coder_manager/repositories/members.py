"""Persistence operations for instance members."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coder_manager.models import Instance, InstanceStatus, Member, MemberStatus, Workspace
from coder_manager.schemas import MemberCreate, MemberRoleUpdate

MAX_ACTION_LENGTH = 255


class MemberAlreadyExistsError(Exception):
    """Raised when a username is already attached to an instance."""


class MemberInstanceNotFoundError(Exception):
    """Raised when a member operation references an unknown instance."""


class MemberNotFoundError(Exception):
    """Raised when an instance member cannot be found."""


class MemberInstanceBusyError(Exception):
    """Raised when an instance action prevents membership changes."""


class MemberActionConflictError(Exception):
    """Raised when a member transition is incompatible with its current state."""


class MemberHasWorkspacesError(Exception):
    """Raised when a member still owns workspaces."""


class InvalidMemberActionError(Exception):
    """Raised when an internal member action is empty or too long."""


class MemberRepository:
    """Store and transition members using an async SQLAlchemy session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the database session used by repository operations."""

        self._session = session

    async def list(
        self,
        instance_id: UUID,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[Member], int]:
        """Return one deterministic page after validating its parent instance."""

        if await self._session.get(Instance, instance_id) is None:
            raise MemberInstanceNotFoundError

        member_filter = Member.instance_id == instance_id
        total = await self._session.scalar(
            select(func.count()).select_from(Member).where(member_filter)
        )
        result = await self._session.scalars(
            select(Member)
            .where(member_filter)
            .order_by(Member.username, Member.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(result), total or 0

    async def get(self, instance_id: UUID, member_id: UUID) -> Member | None:
        """Find a member only within its parent instance."""

        return await self._session.scalar(
            select(Member).where(Member.id == member_id, Member.instance_id == instance_id)
        )

    async def create(self, instance_id: UUID, payload: MemberCreate) -> Member:
        """Add a member and request instance reconciliation when necessary."""

        instance = await self._lock_available_instance(instance_id)
        member = Member(
            instance_id=instance_id,
            username=payload.username,
            role=payload.role,
            action="creating",
            status=MemberStatus.PENDING,
        )
        self._session.add(member)
        enqueue_update = self._request_instance_update(instance)
        if enqueue_update:
            self._session.info["enqueue_instance_update"] = True
        try:
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise MemberAlreadyExistsError from error
        await self._session.refresh(member)
        return member

    async def update_role(
        self,
        instance_id: UUID,
        member_id: UUID,
        payload: MemberRoleUpdate,
    ) -> tuple[Member, bool]:
        """Request a role update and return change and dispatch decisions."""

        instance = await self._lock_available_instance(instance_id)
        member = await self._lock_member(instance_id, member_id)
        if member.status is not MemberStatus.SUCCESS:
            await self._session.rollback()
            raise MemberActionConflictError
        if member.role is payload.role:
            await self._session.commit()
            return member, False

        member.role = payload.role
        member.action = "updating"
        member.status = MemberStatus.PENDING
        enqueue_update = self._request_instance_update(instance)
        if enqueue_update:
            self._session.info["enqueue_instance_update"] = True
        await self._session.commit()
        await self._session.refresh(member)
        return member, True

    async def request_deletion(self, instance_id: UUID, member_id: UUID) -> Member:
        """Move a member to deleting/pending and request reconciliation."""

        instance = await self._lock_available_instance(instance_id)
        member = await self._lock_member(instance_id, member_id)
        if member.status is not MemberStatus.SUCCESS:
            await self._session.rollback()
            raise MemberActionConflictError
        workspace_id = await self._session.scalar(
            select(Workspace.id).where(Workspace.member_id == member.id).limit(1)
        )
        if workspace_id is not None:
            await self._session.rollback()
            raise MemberHasWorkspacesError

        member.action = "deleting"
        member.status = MemberStatus.PENDING
        enqueue_update = self._request_instance_update(instance)
        if enqueue_update:
            self._session.info["enqueue_instance_update"] = True
        await self._session.commit()
        await self._session.refresh(member)
        return member

    async def update_action(
        self,
        member_id: UUID,
        *,
        expected_action: str,
        action: str,
        status: MemberStatus,
    ) -> Member:
        """Update action state while rejecting results from a stale action."""

        normalized_action = action.strip()
        if not normalized_action or len(normalized_action) > MAX_ACTION_LENGTH:
            raise InvalidMemberActionError

        member = await self._session.scalar(
            select(Member).where(Member.id == member_id).with_for_update()
        )
        if member is None:
            await self._session.rollback()
            raise MemberNotFoundError
        if member.action != expected_action:
            await self._session.rollback()
            raise MemberActionConflictError

        member.action = normalized_action
        member.status = status
        await self._session.commit()
        await self._session.refresh(member)
        return member

    async def _lock_available_instance(self, instance_id: UUID) -> Instance:
        """Lock an instance and ensure no instance action is in progress."""

        instance = await self._session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if instance is None:
            await self._session.rollback()
            raise MemberInstanceNotFoundError
        update_in_progress = instance.action == "updating" and instance.status in {
            InstanceStatus.PENDING,
            InstanceStatus.RUNNING,
        }
        if (
            instance.status in {InstanceStatus.PENDING, InstanceStatus.RUNNING}
            and not update_in_progress
        ):
            await self._session.rollback()
            raise MemberInstanceBusyError
        return instance

    @staticmethod
    def _request_instance_update(instance: Instance) -> bool:
        """Mark an idle instance pending and report whether a job must be sent."""

        if instance.action == "updating" and instance.status in {
            InstanceStatus.PENDING,
            InstanceStatus.RUNNING,
        }:
            return False
        instance.action = "updating"
        instance.status = InstanceStatus.PENDING
        return True

    async def _lock_member(self, instance_id: UUID, member_id: UUID) -> Member:
        """Lock one member while enforcing instance ownership."""

        member = await self._session.scalar(
            select(Member)
            .where(Member.id == member_id, Member.instance_id == instance_id)
            .with_for_update()
        )
        if member is None:
            await self._session.rollback()
            raise MemberNotFoundError
        return member
