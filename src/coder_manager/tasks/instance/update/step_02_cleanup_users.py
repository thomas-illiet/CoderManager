"""Remove Coder accounts that are not referenced by active instance members."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from coder_manager import worker_database
from coder_manager.celery_app import celery_app
from coder_manager.config import get_settings
from coder_manager.domains import argocd, coder
from coder_manager.models import Instance, Member, MemberStatus
from coder_manager.tasks.common.execution import (
    ExecutionClaim,
    fail_execution,
    heartbeat_execution,
    required_resource_id,
    run_claimed_step,
)
from coder_manager.tasks.common.registry import INSTANCE_UPDATE_STEP_02_TASK
from coder_manager.tasks.instance._bootstrap import stored_admin_password
from coder_manager.tasks.instance.update.step_01_update_instance import (
    _fail_members,
    _finalize_update,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session, sessionmaker


@celery_app.task(name=INSTANCE_UPDATE_STEP_02_TASK)
def step_02_cleanup_users(job_id: str) -> dict[str, str]:
    """Delete unreferenced Coder users before finalizing local member changes."""

    session_factory = worker_database.get_worker_session_maker()

    def operation(claim: ExecutionClaim) -> dict[str, str]:
        """Compare remote accounts with the manager snapshot and converge them."""

        member_ids, expected_usernames, application_name = _claim_cleanup(
            claim,
            session_factory,
        )
        credentials = stored_admin_password(
            required_resource_id(claim),
            session_factory,
        )
        if credentials is None:
            msg = "Instance administrator password is missing"
            raise RuntimeError(msg)
        try:
            protected_usernames = {
                coder.ADMIN_USERNAME,
                *argocd.parse_default_admins(get_settings().default_admins),
            }
            instance_url, password = credentials
            coder.cleanup_user_accounts(
                instance_url,
                password,
                (*expected_usernames, *protected_usernames),
                heartbeat=lambda: _heartbeat_owned(claim, session_factory),
            )
        except Exception:
            fail_execution(
                claim,
                session_factory,
                mutate=lambda session: _fail_members(session, member_ids),
            )
            raise
        return _finalize_update(
            claim,
            member_ids,
            application_name,
            session_factory,
        )

    return run_claimed_step(job_id, INSTANCE_UPDATE_STEP_02_TASK, session_factory, operation)


def _claim_cleanup(
    claim: ExecutionClaim,
    session_factory: sessionmaker[Session],
) -> tuple[tuple[UUID, ...], tuple[str, ...], str]:
    """Claim reconciled members and build the local username reference set."""

    with session_factory() as session:
        instance = session.get(Instance, claim.resource_id)
        if instance is None:
            msg = "Instance is missing"
            raise RuntimeError(msg)
        if not instance.argocd_application_name:
            msg = "Instance Argo CD application name is missing"
            raise RuntimeError(msg)
        stored_members = list(
            session.scalars(
                select(Member)
                .where(Member.instance_id == instance.id)
                .order_by(Member.username, Member.id)
                .with_for_update()
            )
        )
        claimed_members = [
            member
            for member in stored_members
            if member.status in {MemberStatus.RUNNING, MemberStatus.ERROR}
        ]
        for member in claimed_members:
            member.status = MemberStatus.RUNNING
        claimed_ids = {member.id for member in claimed_members}
        expected_usernames = tuple(
            member.username
            for member in stored_members
            if member.action != "deleting" or member.id not in claimed_ids
        )
        session.commit()
        return (
            tuple(member.id for member in claimed_members),
            expected_usernames,
            instance.argocd_application_name,
        )


def _heartbeat_owned(
    claim: ExecutionClaim,
    session_factory: sessionmaker[Session],
) -> None:
    """Stop remote mutations when this worker loses ownership."""

    if not heartbeat_execution(claim, session_factory):
        msg = "Instance update attempt is no longer current"
        raise RuntimeError(msg)
