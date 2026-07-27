"""Managed database target loading for instance schema steps."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import SecretBytes
from sqlalchemy import select

from coder_manager.config import get_settings
from coder_manager.crypto import KubeconfigCipher, PasswordCipher
from coder_manager.domains.argocd import InstanceHelmValues
from coder_manager.domains.postgresql import SchemaTarget
from coder_manager.models import Database, DatabaseAllocation, InstanceKubernetes

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session, sessionmaker


def database_target(
    instance_id: UUID,
    session_factory: sessionmaker[Session],
) -> SchemaTarget | None:
    """Load and decrypt the database allocation for one instance."""

    with session_factory() as session:
        row = session.execute(
            select(DatabaseAllocation, Database)
            .join(Database, Database.id == DatabaseAllocation.database_id)
            .where(DatabaseAllocation.instance_id == instance_id)
        ).one_or_none()
        if row is None:
            return None
        allocation, database = row
        password = PasswordCipher(get_settings().crypto_key).decrypt(
            database.password_enc,
            database.id,
        )
        return SchemaTarget(
            host=database.host,
            port=database.port,
            database_name=database.database_name,
            username=database.username,
            password=password,
            schema_name=allocation.schema_name,
        )


def instance_helm_values(
    instance_id: UUID,
    slug: str | None,
    environment: str,
    public_url: str,
    session_factory: sessionmaker[Session],
) -> InstanceHelmValues:
    """Load one instance's public URL and decrypted database Helm values."""

    target = database_target(instance_id, session_factory)
    if target is None:
        msg = "Instance database allocation is missing"
        raise RuntimeError(msg)
    with session_factory() as session:
        provider = session.get(InstanceKubernetes, instance_id)
        kubeconfig = (
            None
            if provider is None
            else SecretBytes(
                KubeconfigCipher(get_settings().crypto_key).decrypt(
                    provider.kubeconfig_enc,
                    instance_id,
                )
            )
        )
    return InstanceHelmValues(
        slug=slug,
        environment=environment,
        public_url=public_url,
        database_username=target.username,
        database_password=target.password,
        database_host=target.host,
        database_name=target.database_name,
        database_schema=target.schema_name,
        kubeconfig=kubeconfig,
    )
