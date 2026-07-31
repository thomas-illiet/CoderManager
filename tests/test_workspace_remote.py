"""Remote workspace build polling tests."""

from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from coder_manager.domains.coder import (
    CoderRequestError,
    CoderWorkspace,
    CoderWorkspaceBuild,
)
from coder_manager.tasks.workspace import _remote


def remote_snapshot(
    *,
    workspace_id: UUID | None = None,
) -> _remote.WorkspaceRemoteSnapshot:
    """Build a minimal stable snapshot for pure remote helper tests."""

    return _remote.WorkspaceRemoteSnapshot(
        id=uuid4(),
        name="development",
        username="alice",
        instance_url="https://coder.example.test",
        password=SecretStr("password"),
        coder_template_id=uuid4(),
        coder_workspace_id=workspace_id,
        coder_workspace_build_id=None,
        parameters=(),
        parameters_revision=0,
        applied_parameters_revision=None,
    )


def test_find_remote_workspace_prefers_id_then_owner_name() -> None:
    """Use the persisted UUID first and fall back to owner/name after absence."""

    workspace_id = uuid4()
    build_id = uuid4()
    snapshot = remote_snapshot(workspace_id=workspace_id)
    remote = CoderWorkspace(
        workspace_id,
        "running",
        build_id,
        name="development",
        template_id=snapshot.coder_template_id,
    )

    class FakeClient:
        """Record both supported workspace lookup methods."""

        def __init__(self) -> None:
            """Initialize the configurable result and call log."""

            self.by_id: CoderWorkspace | None = remote
            self.calls: list[object] = []

        def workspace(self, selected_id: object) -> CoderWorkspace | None:
            """Return the configured ID result."""

            self.calls.append(selected_id)
            return self.by_id

        def workspace_by_owner_and_name(
            self,
            username: str,
            name: str,
        ) -> CoderWorkspace:
            """Return the owner/name fallback result."""

            self.calls.append((username, name))
            return remote

    client = FakeClient()
    assert _remote.find_remote_workspace(client, snapshot) == remote  # type: ignore[arg-type]
    assert client.calls == [workspace_id]
    client.by_id = None
    assert _remote.find_remote_workspace(client, snapshot) == remote  # type: ignore[arg-type]
    assert client.calls == [workspace_id, workspace_id, ("alice", "development")]
    _remote.require_matching_template(remote, snapshot)
    wrong_template = CoderWorkspace(
        workspace_id,
        "running",
        build_id,
        name="development",
        template_id=uuid4(),
    )
    with pytest.raises(_remote.WorkspaceRemoteError, match="different template"):
        _remote.require_matching_template(wrong_template, snapshot)


def test_workspace_build_polling_heartbeats_until_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Poll every configured interval and heartbeat before observing success."""

    build_id = uuid4()
    polls: list[object] = []

    class FakeClient:
        """Return one successful build after the initial pending snapshot."""

        def workspace_build(self, selected_build_id: object) -> CoderWorkspaceBuild:
            """Return the terminal running build."""

            polls.append(selected_build_id)
            return CoderWorkspaceBuild(build_id, "running", "start")

    def record_sleep(seconds: float) -> None:
        """Record the configured polling interval without waiting."""

        polls.append(seconds)

    monkeypatch.setattr(_remote.time, "sleep", record_sleep)
    completed = _remote.wait_workspace_build(
        FakeClient(),  # type: ignore[arg-type]
        CoderWorkspaceBuild(build_id, "pending", "start"),
        success_status="running",
        timeout_seconds=10,
        poll_interval_seconds=2,
        heartbeat=lambda: polls.append("heartbeat"),
    )

    assert completed.status == "running"
    assert polls == ["heartbeat", 2, build_id]


def test_workspace_build_polling_times_out_with_sanitized_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop polling at the configured deadline without exposing remote data."""

    build = CoderWorkspaceBuild(uuid4(), "pending", "start")
    clock = iter((0.0, 2.0))
    monkeypatch.setattr(_remote.time, "monotonic", lambda: next(clock))

    with pytest.raises(CoderRequestError, match="workspace build timed out"):
        _remote.wait_workspace_build(
            object(),  # type: ignore[arg-type]
            build,
            success_status="running",
            timeout_seconds=1,
            poll_interval_seconds=2,
        )


@pytest.mark.parametrize("status", ["failed", "canceled", "canceling"])
def test_workspace_build_polling_rejects_terminal_failures(status: str) -> None:
    """Reject terminal remote failures without making another Coder request."""

    with pytest.raises(CoderRequestError, match="workspace build failed"):
        _remote.wait_workspace_build(
            object(),  # type: ignore[arg-type]
            CoderWorkspaceBuild(uuid4(), status, "start"),
            success_status="running",
            timeout_seconds=10,
            poll_interval_seconds=2,
        )
