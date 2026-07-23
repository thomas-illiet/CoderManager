"""Application service for idempotent Coder administrator bootstrap."""

from pydantic import SecretStr

from coder_manager.domains.coder.client import CoderClient


def bootstrap_admin_account(instance_url: str, password: SecretStr) -> None:
    """Create the first user or verify credentials prepared by an earlier attempt."""

    with CoderClient(instance_url) as client:
        if client.has_first_user():
            client.verify_prepared_first_user(password)
        else:
            client.create_first_user(password)
