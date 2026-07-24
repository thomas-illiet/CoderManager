"""Persistence helpers shared by instance administrator bootstrap workflows."""

from secrets import token_urlsafe
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from coder_manager.config import get_settings
from coder_manager.crypto import InstancePasswordCipher
from coder_manager.models import Instance, JobExecution, JobStatus
from coder_manager.tasks.common.registry import INSTANCE_CREATE_STEP_04


def bootstrap_succeeded(session: Session, instance_id: UUID) -> bool:
    """Report whether an instance has a successful administrator bootstrap step."""

    job_id = session.scalar(
        select(JobExecution.id)
        .where(
            JobExecution.resource_type == "instance",
            JobExecution.resource_id == instance_id,
            JobExecution.step == INSTANCE_CREATE_STEP_04,
            JobExecution.status == JobStatus.SUCCESS,
        )
        .limit(1)
    )
    return job_id is not None


def prepared_admin_password(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
) -> tuple[str, SecretStr]:
    """Return the instance URL and a persisted retry-safe administrator password."""

    with session_factory() as session:
        instance = session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if instance is None:
            msg = "Instance is missing"
            raise RuntimeError(msg)
        cipher = InstancePasswordCipher(get_settings().crypto_key)
        if instance.password_enc is None:
            password = SecretStr(token_urlsafe(32))
            instance.password_enc = cipher.encrypt(password, instance.id)
            session.commit()
            return instance.instance_url, password
        password = cipher.decrypt(instance.password_enc, instance.id)
        return instance.instance_url, password
