"""Coder instance member reconciliation task."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.domains import argocd
from coder_manager.models import Instance, InstanceStatus, Member, MemberStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session, sessionmaker

    from coder_manager.tasks._common import JobResult


@dataclass(frozen=True)
class _UpdateClaim:
    """Database snapshot claimed for one external reconciliation pass."""

    member_ids: tuple[UUID, ...]
    active_members: tuple[tuple[str, str], ...]
    attached_name: str | None
    owns_transition: bool = True


@celery_app.task(name="coder_manager.update_instance")
def update_instance(instance_id: str, *, force: bool = False) -> JobResult:
    """Reconcile pending member changes for one Coder instance."""

    return _update_instance(
        UUID(instance_id),
        worker_database.get_worker_session_maker(),
        force=force,
    )


def _update_instance(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
    reconcile: Callable[[UUID, str | None, tuple[tuple[str, str], ...]], str] | None = None,
    *,
    force: bool = False,
) -> JobResult:
    """Claim and reconcile one deterministic batch of pending member changes."""

    # Snapshot and claim pending members before invoking the remote reconciler.
    claim = _claim_update(instance_id, session_factory, force=force)
    if claim is None:
        return {"status": "noop"}

    argocd_application_name = _reconcile_claim(
        instance_id,
        claim,
        session_factory,
        reconcile,
    )

    if not claim.owns_transition:
        return {"status": "success"}

    # Finalize only the claimed members, preserving changes queued during reconciliation.
    enqueue_next_pass = False
    with session_factory() as session:
        instance = session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if (
            instance is None
            or instance.action != "updating"
            or instance.status is not InstanceStatus.RUNNING
        ):
            return {"status": "noop"}
        instance.argocd_application_name = argocd_application_name

        if claim.member_ids:
            claimed_members = list(
                session.scalars(
                    select(Member)
                    .where(
                        Member.id.in_(claim.member_ids),
                        Member.instance_id == instance_id,
                        Member.status == MemberStatus.RUNNING,
                    )
                    .with_for_update()
                )
            )
            for member in claimed_members:
                if member.action == "deleting":
                    session.delete(member)
                else:
                    member.status = MemberStatus.SUCCESS

        # A second pass coalesces any membership changes that arrived during this run.
        pending_member_id = session.scalar(
            select(Member.id)
            .where(
                Member.instance_id == instance_id,
                Member.status == MemberStatus.PENDING,
            )
            .limit(1)
        )
        if pending_member_id is None:
            instance.status = InstanceStatus.SUCCESS
            result = {"status": "success"}
        else:
            instance.status = InstanceStatus.PENDING
            enqueue_next_pass = True
            result = {"status": "pending"}
        session.commit()

    if enqueue_next_pass:
        update_instance.delay(str(instance_id))
    return result


def _reconcile_claim(
    instance_id: UUID,
    claim: _UpdateClaim,
    session_factory: sessionmaker[Session],
    reconcile: Callable[[UUID, str | None, tuple[tuple[str, str], ...]], str] | None,
) -> str:
    """Reconcile outside the transaction and fail only an owned transition."""

    try:
        reconcile_operation = reconcile or argocd.reconcile_instance_application
        return reconcile_operation(
            instance_id,
            claim.attached_name,
            claim.active_members,
        )
    except Exception:
        if claim.owns_transition:
            _mark_instance_update_error(instance_id, list(claim.member_ids), session_factory)
        raise


def _claim_update(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
    *,
    force: bool = False,
) -> _UpdateClaim | None:
    """Move an eligible instance and its pending members into running state."""

    with session_factory() as session:
        instance = session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if instance is None or instance.action != "updating":
            return None
        if instance.status is not InstanceStatus.PENDING:
            if not force:
                return None
            members = list(
                session.scalars(
                    select(Member)
                    .where(Member.instance_id == instance_id)
                    .order_by(Member.username, Member.id)
                )
            )
            return _UpdateClaim(
                member_ids=(),
                active_members=tuple(
                    (member.username, member.role.value)
                    for member in members
                    if member.action != "deleting"
                ),
                attached_name=instance.argocd_application_name,
                owns_transition=False,
            )
        instance.status = InstanceStatus.RUNNING
        members = list(
            session.scalars(
                select(Member)
                .where(Member.instance_id == instance_id)
                .order_by(Member.username, Member.id)
                .with_for_update()
            )
        )
        # Claim only pending members while retaining every active member in desired state.
        claimed_member_ids: list[UUID] = []
        for member in members:
            if member.status is MemberStatus.PENDING:
                member.status = MemberStatus.RUNNING
                claimed_member_ids.append(member.id)
        claim = _UpdateClaim(
            member_ids=tuple(claimed_member_ids),
            active_members=tuple(
                (member.username, member.role.value)
                for member in members
                if member.action != "deleting"
            ),
            attached_name=instance.argocd_application_name,
        )
        session.commit()
        return claim


def _mark_instance_update_error(
    instance_id: UUID,
    claimed_member_ids: list[UUID],
    session_factory: sessionmaker[Session],
) -> None:
    """Mark the instance and its claimed member batch as failed."""

    with session_factory() as session:
        instance = session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if (
            instance is not None
            and instance.action == "updating"
            and instance.status is InstanceStatus.RUNNING
        ):
            instance.status = InstanceStatus.ERROR
        if claimed_member_ids:
            members = session.scalars(
                select(Member).where(
                    Member.id.in_(claimed_member_ids),
                    Member.instance_id == instance_id,
                    Member.status == MemberStatus.RUNNING,
                )
            )
            for member in members:
                member.status = MemberStatus.ERROR
        session.commit()
