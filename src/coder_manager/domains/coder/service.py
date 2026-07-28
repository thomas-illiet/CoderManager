"""Application services for Coder administrator and user account lifecycle."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from coder_manager.domains.coder.client import CoderClient
from coder_manager.domains.coder.errors import CoderRequestError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from uuid import UUID

    from pydantic import SecretStr

    from coder_manager.domains.coder.models import CoderWorkspace

WORKSPACE_PAGE_SIZE = 100
ACTIVE_WORKSPACE_STATUSES = ("running", "starting")
OBSERVED_WORKSPACE_STATUSES = (*ACTIVE_WORKSPACE_STATUSES, "stopping")
TERMINAL_WORKSPACE_STOP_FAILURES = frozenset(
    {"failed", "canceled", "canceling", "deleting", "deleted"}
)


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


def stop_active_workspaces(
    instance_url: str,
    password: SecretStr,
    *,
    timeout_seconds: int,
    poll_interval_seconds: float,
    heartbeat: Callable[[], None] | None = None,
) -> tuple[str, ...]:
    """Stop every running or starting remote workspace before instance shutdown."""

    deadline = time.monotonic() + timeout_seconds
    stopped_ids: list[str] = []
    with CoderClient(instance_url) as client:
        client.authenticate_prepared_admin(password)
        while True:
            if time.monotonic() >= deadline:
                msg = "Coder workspace stop timed out"
                raise CoderRequestError(msg)
            active = _active_workspaces(client, heartbeat)
            if not active:
                return tuple(stopped_ids)
            pending_builds = set()
            for workspace in active:
                _heartbeat(heartbeat)
                if workspace.status == "stopping":
                    pending_builds.add(workspace.latest_build_id)
                else:
                    build = client.create_workspace_stop_build(workspace.id)
                    if build.status != "stopped":
                        pending_builds.add(build.id)
                stopped_ids.append(str(workspace.id))
            _wait_workspace_builds(
                client,
                pending_builds,
                deadline=deadline,
                poll_interval_seconds=poll_interval_seconds,
                heartbeat=heartbeat,
            )


def _wait_workspace_builds(
    client: CoderClient,
    pending: set[UUID],
    *,
    deadline: float,
    poll_interval_seconds: float,
    heartbeat: Callable[[], None] | None,
) -> None:
    """Wait for every submitted stop build under one global deadline."""

    while pending:
        if time.monotonic() >= deadline:
            msg = "Coder workspace stop timed out"
            raise CoderRequestError(msg)
        _heartbeat(heartbeat)
        for build_id in tuple(pending):
            build = client.workspace_build(build_id)
            if build.status == "stopped":
                pending.remove(build_id)
            elif build.status in TERMINAL_WORKSPACE_STOP_FAILURES:
                msg = "Coder workspace stop failed"
                raise CoderRequestError(msg)
        if pending:
            time.sleep(poll_interval_seconds)


def _active_workspaces(
    client: CoderClient,
    heartbeat: Callable[[], None] | None,
) -> tuple[CoderWorkspace, ...]:
    """Load every running or starting workspace with strict pagination."""

    found: dict[str, CoderWorkspace] = {}
    for status in OBSERVED_WORKSPACE_STATUSES:
        offset = 0
        while True:
            _heartbeat(heartbeat)
            page = client.workspaces(
                status=status,
                offset=offset,
                limit=WORKSPACE_PAGE_SIZE,
            )
            for workspace in page.items:
                found[str(workspace.id)] = workspace
            offset += len(page.items)
            if offset >= page.count:
                break
            if not page.items:
                msg = "Coder workspace pagination did not advance"
                raise CoderRequestError(msg)
    return tuple(found[key] for key in sorted(found))


def _heartbeat(callback: Callable[[], None] | None) -> None:
    """Refresh the durable claim when a callback is configured."""

    if callback is not None:
        callback()
