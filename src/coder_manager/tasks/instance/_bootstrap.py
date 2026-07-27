"""Persistence helpers shared by instance administrator workflows."""

from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from coder_manager.config import get_settings
from coder_manager.crypto import InstancePasswordCipher
from coder_manager.models import Instance


def store_verified_admin_password(instance: Instance, password: SecretStr) -> None:
    """Encrypt and store a password verified by the remote bootstrap."""

    if instance.password_enc is None:
        instance.password_enc = InstancePasswordCipher(get_settings().crypto_key).encrypt(
            password,
            instance.id,
        )


def stored_admin_password(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
) -> tuple[str, SecretStr] | None:
    """Return the instance URL and stored administrator password when configured."""

    with session_factory() as session:
        instance = session.scalar(select(Instance).where(Instance.id == instance_id))
        if instance is None:
            msg = "Instance is missing"
            raise RuntimeError(msg)
        if instance.password_enc is None:
            return None
        password = InstancePasswordCipher(get_settings().crypto_key).decrypt(
            instance.password_enc,
            instance.id,
        )
        return instance.instance_url, password
