"""Application services for Coder administrator and user account lifecycle."""

from collections.abc import Callable, Iterable

from pydantic import SecretStr

from coder_manager.domains.coder.client import CoderClient


def bootstrap_admin_account(instance_url: str, password: SecretStr) -> None:
    """Create the first user or verify credentials prepared by an earlier attempt."""

    with CoderClient(instance_url) as client:
        if client.has_first_user():
            client.verify_prepared_first_user(password)
        else:
            client.create_first_user(password)


def delete_user_accounts(
    instance_url: str,
    password: SecretStr,
    usernames: Iterable[str],
    *,
    heartbeat: Callable[[], None] | None = None,
) -> None:
    """Authenticate the prepared administrator and delete users idempotently."""

    with CoderClient(instance_url) as client:
        if not client.has_first_user():
            client.create_first_user(password)
        client.authenticate_prepared_admin(password)
        for username in usernames:
            if heartbeat is not None:
                heartbeat()
            client.delete_user(username)


def cleanup_user_accounts(
    instance_url: str,
    password: SecretStr,
    expected_usernames: Iterable[str],
    *,
    heartbeat: Callable[[], None] | None = None,
) -> tuple[str, ...]:
    """Delete every Coder account not present in the expected username set."""

    with CoderClient(instance_url) as client:
        if not client.has_first_user():
            client.create_first_user(password)
        client.authenticate_prepared_admin(password)
        if heartbeat is not None:
            heartbeat()
        expected = set(expected_usernames)
        orphaned = tuple(sorted(set(client.usernames()) - expected))
        for username in orphaned:
            if heartbeat is not None:
                heartbeat()
            client.delete_user(username)
        return orphaned
