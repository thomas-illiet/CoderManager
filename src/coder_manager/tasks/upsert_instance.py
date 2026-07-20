"""Create or update one Coder instance through a single Argo CD upsert."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.domains import argocd
from coder_manager.models import Instance, InstanceStatus, Member, MemberStatus
from coder_manager.tasks._common import StatefulResourceTask

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session, sessionmaker

    from coder_manager.tasks._common import JobResult

UPSERT_ACTIONS = ("creating", "updating")


@dataclass(frozen=True)
class _UpsertClaim:
    """Database snapshot owned by one Argo CD reconciliation pass."""

    action: str
    member_ids: tuple[UUID, ...]
    active_members: tuple[tuple[str, str], ...]
    attached_name: str | None
    region: str
    environment: str


@celery_app.task(
    name="coder_manager.upsert_instance",
    base=StatefulResourceTask,
    resource_type="instance",
    actions=UPSERT_ACTIONS,
    fail_running_members=True,
)
def upsert_instance(instance_id: str, *, retry_error: bool = False) -> JobResult:
    """Create or update an instance, optionally reclaiming an errored transition."""

    return _upsert_instance(
        UUID(instance_id),
        worker_database.get_worker_session_maker(),
        retry_error=retry_error,
    )


def _upsert_instance(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
    reconcile: Callable[[UUID, str | None, tuple[tuple[str, str], ...], str, str], str]
    | None = None,
    *,
    retry_error: bool = False,
) -> JobResult:
    """Claim, reconcile, and finalize one deterministic instance snapshot."""

    claim = _claim_upsert(instance_id, session_factory, retry_error=retry_error)
    if claim is None:
        return {"status": "noop"}

    try:
        reconcile_operation = reconcile or argocd.reconcile_instance_application
        application_name = reconcile_operation(
            instance_id,
            claim.attached_name,
            claim.active_members,
            claim.region,
            claim.environment,
        )
    except Exception:
        _mark_upsert_error(instance_id, claim, session_factory)
        raise

    enqueue_next_pass = False
    with session_factory() as session:
        instance = session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if (
            instance is None
            or instance.action != claim.action
            or instance.status is not InstanceStatus.RUNNING
        ):
            return {"status": "noop"}

        instance.argocd_application_name = application_name
        _finalize_members(instance_id, claim.member_ids, session)

        pending_member = session.scalar(
            select(Member.id)
            .where(
                Member.instance_id == instance_id,
                Member.status == MemberStatus.PENDING,
            )
            .limit(1)
        )
        if pending_member is None:
            instance.status = InstanceStatus.SUCCESS
            result = {"status": "success"}
        else:
            instance.action = "updating"
            instance.status = InstanceStatus.PENDING
            enqueue_next_pass = True
            result = {"status": "pending"}
        session.commit()

    if enqueue_next_pass:
        upsert_instance.delay(str(instance_id))
    return result


def _claim_upsert(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
    *,
    retry_error: bool,
) -> _UpsertClaim | None:
    """Claim a pending upsert or an explicit retry directly under the row lock."""

    with session_factory() as session:
        instance = session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if instance is None or instance.action not in UPSERT_ACTIONS:
            return None
        retryable_error = retry_error and instance.status is InstanceStatus.ERROR
        if instance.status is not InstanceStatus.PENDING and not retryable_error:
            return None

        instance.status = InstanceStatus.RUNNING
        members = list(
            session.scalars(
                select(Member)
                .where(Member.instance_id == instance_id)
                .order_by(Member.username, Member.id)
                .with_for_update()
            )
        )
        claimed_member_ids: list[UUID] = []
        for member in members:
            if member.status is MemberStatus.PENDING or (
                retryable_error and member.status is MemberStatus.ERROR
            ):
                member.status = MemberStatus.RUNNING
                claimed_member_ids.append(member.id)

        claim = _UpsertClaim(
            action=instance.action,
            member_ids=tuple(claimed_member_ids),
            active_members=tuple(
                (member.username, member.role.value)
                for member in members
                if member.action != "deleting"
            ),
            attached_name=instance.argocd_application_name,
            region=instance.region.value,
            environment=instance.environment.value,
        )
        session.commit()
        return claim


def _finalize_members(
    instance_id: UUID,
    member_ids: tuple[UUID, ...],
    session: Session,
) -> None:
    """Finalize only members claimed by this upsert pass."""

    if not member_ids:
        return
    members = session.scalars(
        select(Member)
        .where(
            Member.id.in_(member_ids),
            Member.instance_id == instance_id,
            Member.status == MemberStatus.RUNNING,
        )
        .with_for_update()
    )
    for member in members:
        if member.action == "deleting":
            session.delete(member)
        else:
            member.status = MemberStatus.SUCCESS


def _mark_upsert_error(
    instance_id: UUID,
    claim: _UpsertClaim,
    session_factory: sessionmaker[Session],
) -> None:
    """Keep the claimed instance and members retryable after an Argo CD error."""

    with session_factory() as session:
        instance = session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if (
            instance is not None
            and instance.action == claim.action
            and instance.status is InstanceStatus.RUNNING
        ):
            instance.status = InstanceStatus.ERROR
        if claim.member_ids:
            members = session.scalars(
                select(Member).where(
                    Member.id.in_(claim.member_ids),
                    Member.instance_id == instance_id,
                    Member.status == MemberStatus.RUNNING,
                )
            )
            for member in members:
                member.status = MemberStatus.ERROR
        session.commit()
