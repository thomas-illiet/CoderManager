"""Application services for Coder administrator and user account lifecycle."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx

from coder_manager.domains.coder.client import CoderClient
from coder_manager.domains.coder.errors import CoderFirstUserConflictError, CoderRequestError

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
ACTIVE_WORKSPACE_BUILD_STATUSES = frozenset(
    {"pending", "starting", "stopping", "canceling", "deleting"}
)
TERMINAL_WORKSPACE_DELETE_FAILURES = frozenset({"failed", "canceled"})


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
                    if build.transition != "stop":
                        msg = "Coder workspace stop returned an unexpected transition"
                        raise CoderRequestError(msg)
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
            if build.transition != "stop":
                msg = "Coder workspace stop returned an unexpected transition"
                raise CoderRequestError(msg)
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


def delete_all_workspaces(
    instance_url: str,
    password: SecretStr,
    *,
    timeout_seconds: int,
    poll_interval_seconds: float,
    heartbeat: Callable[[], None] | None = None,
) -> tuple[str, ...]:
    """Delete every remote workspace and prove that no active row remains."""

    deadline = time.monotonic() + timeout_seconds
    deleted_ids: set[str] = set()
    client = _wait_for_coder(
        instance_url,
        password,
        deadline=deadline,
        poll_interval_seconds=poll_interval_seconds,
        heartbeat=heartbeat,
    )
    try:
        while True:
            _require_before_deadline(deadline, "Coder workspace deletion timed out")
            workspaces = _all_workspaces(client, heartbeat)
            if not workspaces:
                return tuple(sorted(deleted_ids))

            blocking_builds: set[UUID] = set()
            delete_builds: set[UUID] = set()
            for workspace in workspaces:
                _heartbeat(heartbeat)
                deleted_ids.add(str(workspace.id))
                if workspace.status in ACTIVE_WORKSPACE_BUILD_STATUSES:
                    if workspace.latest_build_transition == "delete":
                        delete_builds.add(workspace.latest_build_id)
                    else:
                        blocking_builds.add(workspace.latest_build_id)
                    continue

                build = client.create_workspace_delete_build(workspace.id)
                if build.transition != "delete":
                    msg = "Coder workspace deletion returned an unexpected transition"
                    raise CoderRequestError(msg)
                if build.status in TERMINAL_WORKSPACE_DELETE_FAILURES:
                    msg = "Coder workspace deletion failed"
                    raise CoderRequestError(msg)
                if build.status != "deleted":
                    delete_builds.add(build.id)

            _wait_workspace_deletions(
                client,
                blocking_builds,
                delete_builds,
                deadline=deadline,
                poll_interval_seconds=poll_interval_seconds,
                heartbeat=heartbeat,
            )
    finally:
        client.close()


def _wait_for_coder(
    instance_url: str,
    password: SecretStr,
    *,
    deadline: float,
    poll_interval_seconds: float,
    heartbeat: Callable[[], None] | None,
) -> CoderClient:
    """Wait until a restored Coder instance accepts the prepared administrator."""

    while True:
        _require_before_deadline(deadline, "Coder workspace deletion timed out")
        _heartbeat(heartbeat)
        client = CoderClient(instance_url)
        try:
            client.authenticate_prepared_admin(password)
        except CoderFirstUserConflictError:
            client.close()
            raise
        except (CoderRequestError, httpx.HTTPError) as error:
            client.close()
            if time.monotonic() >= deadline:
                msg = "Coder workspace deletion timed out"
                raise CoderRequestError(msg) from error
            time.sleep(poll_interval_seconds)
            continue
        return client


def _all_workspaces(
    client: CoderClient,
    heartbeat: Callable[[], None] | None,
) -> tuple[CoderWorkspace, ...]:
    """Load one complete stable snapshot of every non-deleted workspace."""

    found: dict[str, CoderWorkspace] = {}
    expected_count: int | None = None
    offset = 0
    while True:
        _heartbeat(heartbeat)
        page = client.workspaces(status=None, offset=offset, limit=WORKSPACE_PAGE_SIZE)
        if expected_count is None:
            expected_count = page.count
        elif page.count != expected_count:
            msg = "Coder workspace count changed during pagination"
            raise CoderRequestError(msg)
        for workspace in page.items:
            key = str(workspace.id)
            if key in found:
                msg = "Coder workspace pagination returned a duplicate workspace"
                raise CoderRequestError(msg)
            found[key] = workspace
        offset += len(page.items)
        if offset >= page.count:
            break
        if not page.items:
            msg = "Coder workspace pagination did not advance"
            raise CoderRequestError(msg)
    if expected_count != len(found):
        msg = "Coder workspace pagination returned an incomplete snapshot"
        raise CoderRequestError(msg)
    return tuple(found[key] for key in sorted(found))


def _wait_workspace_deletions(  # noqa: PLR0913
    client: CoderClient,
    blocking: set[UUID],
    deleting: set[UUID],
    *,
    deadline: float,
    poll_interval_seconds: float,
    heartbeat: Callable[[], None] | None,
) -> None:
    """Wait for active builds and all submitted delete builds."""

    while blocking or deleting:
        _require_before_deadline(deadline, "Coder workspace deletion timed out")
        _heartbeat(heartbeat)
        for build_id in tuple(blocking):
            build = client.workspace_build(build_id)
            if build.status not in ACTIVE_WORKSPACE_BUILD_STATUSES:
                blocking.remove(build_id)
        for build_id in tuple(deleting):
            build = client.workspace_build(build_id)
            if build.transition != "delete":
                msg = "Coder workspace deletion returned an unexpected transition"
                raise CoderRequestError(msg)
            if build.status == "deleted":
                deleting.remove(build_id)
            elif build.status in TERMINAL_WORKSPACE_DELETE_FAILURES:
                msg = "Coder workspace deletion failed"
                raise CoderRequestError(msg)
        if blocking or deleting:
            time.sleep(poll_interval_seconds)


def _require_before_deadline(deadline: float, message: str) -> None:
    """Raise a sanitized timeout once the shared deadline is exhausted."""

    if time.monotonic() >= deadline:
        raise CoderRequestError(message)


def _heartbeat(callback: Callable[[], None] | None) -> None:
    """Refresh the durable claim when a callback is configured."""

    if callback is not None:
        callback()
