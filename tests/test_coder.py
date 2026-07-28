"""Coder first-user bootstrap HTTP contract tests."""

import json
from typing import Self
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from coder_manager.domains.coder import (
    ADMIN_EMAIL,
    ADMIN_NAME,
    ADMIN_USERNAME,
    CoderClient,
    CoderFirstUserConflictError,
    CoderRequestError,
    CoderWorkspace,
    CoderWorkspaceBuild,
    CoderWorkspacePage,
    cleanup_user_accounts,
    delete_user_accounts,
    stop_active_workspaces,
)
from coder_manager.domains.coder import client as coder_client
from coder_manager.domains.coder import service as coder_service

PASSWORD = SecretStr("prepared-secret-password")


def test_client_disables_tls_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable certificate verification for calls to managed Coder instances."""

    captured: dict[str, object] = {}

    class StubClient:
        """Capture the HTTP client configuration."""

        def __init__(self, **kwargs: object) -> None:
            """Record the arguments passed to the HTTP client."""

            captured.update(kwargs)

        def close(self) -> None:
            """Record that the client was closed."""

            captured["closed"] = True

    monkeypatch.setattr(coder_client.httpx, "Client", StubClient)
    client = CoderClient("https://coder.example.test")
    client.close()

    assert captured["verify"] is False
    assert captured["closed"] is True


def test_workspace_stop_http_contract_and_strict_response_validation() -> None:
    """List active workspaces, submit a stop build, and observe its result."""

    workspace_id = uuid4()
    build_id = uuid4()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return strict workspace and build payloads."""

        requests.append(request)
        if request.url.path.endswith("/workspaces"):
            return httpx.Response(
                200,
                json={
                    "workspaces": [
                        {
                            "id": str(workspace_id),
                            "latest_build": {
                                "id": str(build_id),
                                "status": "running",
                            },
                        }
                    ],
                    "count": 1,
                },
            )
        if request.method == "POST":
            return httpx.Response(
                201,
                json={"id": str(build_id), "status": "stopping"},
            )
        return httpx.Response(
            200,
            json={"id": str(build_id), "status": "stopped"},
        )

    with CoderClient(
        "https://coder.example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        page = client.workspaces(status="running", offset=0, limit=100)
        submitted = client.create_workspace_stop_build(workspace_id)
        completed = client.workspace_build(submitted.id)

    assert page == CoderWorkspacePage(
        items=(
            CoderWorkspace(
                id=workspace_id,
                status="running",
                latest_build_id=build_id,
            ),
        ),
        count=1,
    )
    assert completed == CoderWorkspaceBuild(id=build_id, status="stopped")
    assert requests[0].url.params["q"] == 'status:"running"'
    assert requests[0].url.params["offset"] == "0"
    assert requests[0].url.params["limit"] == "100"
    assert json.loads(requests[1].content) == {"transition": "stop"}


def test_workspace_page_rejects_incomplete_coder_response() -> None:
    """Reject a page whose item count contradicts Coder's total count."""

    workspace_id = uuid4()
    build_id = uuid4()

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return one item while claiming that two should be present."""

        return httpx.Response(
            200,
            json={
                "workspaces": [
                    {
                        "id": str(workspace_id),
                        "latest_build": {
                            "id": str(build_id),
                            "status": "running",
                        },
                    }
                ],
                "count": 2,
            },
        )

    with (
        CoderClient(
            "https://coder.example.test",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(CoderRequestError, match="incomplete workspace page"),
    ):
        client.workspaces(status="running", offset=0, limit=100)


def test_stop_active_workspaces_waits_and_rechecks_until_none_are_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submit all running and starting stops, wait, then perform a final scan."""

    running_id = uuid4()
    starting_id = uuid4()
    build_ids = {running_id: uuid4(), starting_id: uuid4()}
    status_reads = {"running": 0, "starting": 0, "stopping": 0}
    heartbeats: list[str] = []

    class StubClient:
        """Simulate two active workspaces followed by an empty final scan."""

        def __init__(self, instance_url: str) -> None:
            """Capture the strict instance URL."""

            assert instance_url == "https://coder.example.test"

        def __enter__(self) -> Self:
            """Enter the fake client."""

            return self

        def __exit__(self, *_args: object) -> None:
            """Exit the fake client."""

        def authenticate_prepared_admin(self, password: SecretStr) -> None:
            """Require the prepared password."""

            assert password.get_secret_value() == PASSWORD.get_secret_value()

        def workspaces(self, *, status: str, offset: int, limit: int) -> CoderWorkspacePage:
            """Return one matching active workspace only on the first scan."""

            assert offset == 0
            assert limit == 100
            status_reads[status] += 1
            if status == "stopping" or status_reads[status] > 1:
                return CoderWorkspacePage(items=(), count=0)
            workspace_id = running_id if status == "running" else starting_id
            return CoderWorkspacePage(
                items=(
                    CoderWorkspace(
                        id=workspace_id,
                        status=status,
                        latest_build_id=build_ids[workspace_id],
                    ),
                ),
                count=1,
            )

        def create_workspace_stop_build(self, workspace_id: UUID) -> CoderWorkspaceBuild:
            """Queue one strict stop build."""

            return CoderWorkspaceBuild(id=build_ids[workspace_id], status="stopping")

        def workspace_build(self, build_id: UUID) -> CoderWorkspaceBuild:
            """Complete every submitted stop build."""

            assert build_id in build_ids.values()
            return CoderWorkspaceBuild(id=build_id, status="stopped")

    monkeypatch.setattr(coder_service, "CoderClient", StubClient)
    stopped = stop_active_workspaces(
        "https://coder.example.test",
        PASSWORD,
        timeout_seconds=5,
        poll_interval_seconds=0.001,
        heartbeat=lambda: heartbeats.append("beat"),
    )

    assert set(stopped) == {str(running_id), str(starting_id)}
    assert status_reads == {"running": 2, "starting": 2, "stopping": 2}
    assert heartbeats


def test_stop_active_workspaces_waits_for_build_submitted_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wait for an existing stopping build without submitting a duplicate."""

    workspace_id = uuid4()
    build_id = uuid4()
    stopping_reads = 0

    class StubClient:
        """Expose one stopping workspace from an earlier attempt."""

        def __init__(self, _instance_url: str) -> None:
            """Initialize the fake client."""

        def __enter__(self) -> Self:
            """Enter the fake client."""

            return self

        def __exit__(self, *_args: object) -> None:
            """Exit the fake client."""

        def authenticate_prepared_admin(self, _password: SecretStr) -> None:
            """Accept the configured password."""

        def workspaces(self, *, status: str, offset: int, limit: int) -> CoderWorkspacePage:
            """Return the existing stopping build only during the first scan."""

            nonlocal stopping_reads
            assert offset == 0
            assert limit == 100
            if status != "stopping":
                return CoderWorkspacePage(items=(), count=0)
            stopping_reads += 1
            if stopping_reads > 1:
                return CoderWorkspacePage(items=(), count=0)
            return CoderWorkspacePage(
                items=(
                    CoderWorkspace(
                        id=workspace_id,
                        status=status,
                        latest_build_id=build_id,
                    ),
                ),
                count=1,
            )

        def create_workspace_stop_build(self, _workspace_id: UUID) -> CoderWorkspaceBuild:
            """Reject a duplicate stop submission."""

            pytest.fail("stopping workspace must not receive another build")

        def workspace_build(self, observed_build_id: UUID) -> CoderWorkspaceBuild:
            """Complete the build created by the earlier attempt."""

            assert observed_build_id == build_id
            return CoderWorkspaceBuild(id=build_id, status="stopped")

    monkeypatch.setattr(coder_service, "CoderClient", StubClient)
    assert stop_active_workspaces(
        "https://coder.example.test",
        PASSWORD,
        timeout_seconds=5,
        poll_interval_seconds=0.001,
    ) == (str(workspace_id),)


def test_stop_active_workspaces_paginates_every_active_workspace(  # noqa: C901
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submit stop builds for all pages before the final empty rescan."""

    workspace_ids = (uuid4(), uuid4())
    running_scan = 0
    submitted: list[UUID] = []

    class StubClient:
        """Expose two one-item pages during the first running scan."""

        def __init__(self, _instance_url: str) -> None:
            """Initialize the fake client."""

        def __enter__(self) -> Self:
            """Enter the fake client."""

            return self

        def __exit__(self, *_args: object) -> None:
            """Exit the fake client."""

        def authenticate_prepared_admin(self, _password: SecretStr) -> None:
            """Accept the configured password."""

        def workspaces(self, *, status: str, offset: int, limit: int) -> CoderWorkspacePage:
            """Return each running page once and empty subsequent scans."""

            nonlocal running_scan
            assert limit == 1
            if status != "running":
                return CoderWorkspacePage(items=(), count=0)
            if offset == 0:
                running_scan += 1
            if running_scan > 1:
                return CoderWorkspacePage(items=(), count=0)
            return CoderWorkspacePage(
                items=(
                    CoderWorkspace(
                        id=workspace_ids[offset],
                        status=status,
                        latest_build_id=uuid4(),
                    ),
                ),
                count=2,
            )

        def create_workspace_stop_build(self, workspace_id: UUID) -> CoderWorkspaceBuild:
            """Record one stop per paginated workspace."""

            submitted.append(workspace_id)
            return CoderWorkspaceBuild(id=uuid4(), status="stopped")

        def workspace_build(self, _build_id: UUID) -> CoderWorkspaceBuild:
            """Reject polling because submitted builds are already stopped."""

            pytest.fail("completed builds must not be polled")

    monkeypatch.setattr(coder_service, "CoderClient", StubClient)
    monkeypatch.setattr(coder_service, "WORKSPACE_PAGE_SIZE", 1)
    stopped = stop_active_workspaces(
        "https://coder.example.test",
        PASSWORD,
        timeout_seconds=5,
        poll_interval_seconds=0.001,
    )

    assert set(submitted) == set(workspace_ids)
    assert set(stopped) == {str(workspace_id) for workspace_id in workspace_ids}


def test_stop_active_workspaces_rejects_terminal_build_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the instance running when a submitted stop build fails terminally."""

    workspace_id = uuid4()
    build_id = uuid4()

    class StubClient:
        """Expose one running workspace whose stop build fails."""

        def __init__(self, _instance_url: str) -> None:
            """Initialize the fake client."""

        def __enter__(self) -> Self:
            """Enter the fake client."""

            return self

        def __exit__(self, *_args: object) -> None:
            """Exit the fake client."""

        def authenticate_prepared_admin(self, _password: SecretStr) -> None:
            """Accept the configured password."""

        def workspaces(self, *, status: str, offset: int, limit: int) -> CoderWorkspacePage:
            """Return one running workspace and no starting workspace."""

            assert offset == 0
            assert limit == 100
            if status == "running":
                return CoderWorkspacePage(
                    items=(
                        CoderWorkspace(
                            id=workspace_id,
                            status=status,
                            latest_build_id=build_id,
                        ),
                    ),
                    count=1,
                )
            return CoderWorkspacePage(items=(), count=0)

        def create_workspace_stop_build(self, _workspace_id: UUID) -> CoderWorkspaceBuild:
            """Return the submitted build."""

            return CoderWorkspaceBuild(id=build_id, status="stopping")

        def workspace_build(self, _build_id: UUID) -> CoderWorkspaceBuild:
            """Return a terminal failure."""

            return CoderWorkspaceBuild(id=build_id, status="failed")

    monkeypatch.setattr(coder_service, "CoderClient", StubClient)
    with pytest.raises(CoderRequestError, match="workspace stop failed"):
        stop_active_workspaces(
            "https://coder.example.test",
            PASSWORD,
            timeout_seconds=5,
            poll_interval_seconds=0.001,
        )


def test_stop_active_workspaces_uses_one_global_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply the configured deadline while polling submitted stop builds."""

    workspace_id = uuid4()
    build_id = uuid4()
    monotonic_values = iter((0.0, 0.0, 1.0))

    class StubClient:
        """Expose one build that remains in progress until the deadline."""

        def __init__(self, _instance_url: str) -> None:
            """Initialize the fake client."""

        def __enter__(self) -> Self:
            """Enter the fake client."""

            return self

        def __exit__(self, *_args: object) -> None:
            """Exit the fake client."""

        def authenticate_prepared_admin(self, _password: SecretStr) -> None:
            """Accept the configured password."""

        def workspaces(self, *, status: str, offset: int, limit: int) -> CoderWorkspacePage:
            """Return one running workspace and no starting workspace."""

            assert offset == 0
            assert limit == 100
            if status == "running":
                return CoderWorkspacePage(
                    items=(
                        CoderWorkspace(
                            id=workspace_id,
                            status=status,
                            latest_build_id=build_id,
                        ),
                    ),
                    count=1,
                )
            return CoderWorkspacePage(items=(), count=0)

        def create_workspace_stop_build(self, _workspace_id: UUID) -> CoderWorkspaceBuild:
            """Return the submitted build."""

            return CoderWorkspaceBuild(id=build_id, status="stopping")

        def workspace_build(self, _build_id: UUID) -> CoderWorkspaceBuild:
            """Keep the build pending."""

            return CoderWorkspaceBuild(id=build_id, status="stopping")

    monkeypatch.setattr(coder_service, "CoderClient", StubClient)
    monkeypatch.setattr(coder_service.time, "monotonic", lambda: next(monotonic_values))
    with pytest.raises(CoderRequestError, match="workspace stop timed out"):
        stop_active_workspaces(
            "https://coder.example.test",
            PASSWORD,
            timeout_seconds=1,
            poll_interval_seconds=0.001,
        )


def test_create_first_user_contract() -> None:
    """Detect an empty Coder deployment and create the static administrator."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return the empty-deployment responses expected by the client."""

        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                httpx.codes.NOT_FOUND,
                headers={"X-Coder-Build-Version": "v2.35.2"},
            )
        return httpx.Response(httpx.codes.CREATED, json={"user_id": "ignored"})

    with CoderClient(
        "https://coder.example.test/root",
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.has_first_user() is False
        client.create_first_user(PASSWORD)

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/root/api/v2/users/first"),
        ("POST", "/root/api/v2/users/first"),
    ]
    assert json.loads(requests[1].content) == {
        "email": ADMIN_EMAIL,
        "username": ADMIN_USERNAME,
        "name": ADMIN_NAME,
        "password": PASSWORD.get_secret_value(),
        "trial": False,
    }


def test_recover_prepared_first_user_without_persisting_session() -> None:
    """Authenticate prepared credentials after a remote-success/local-failure window."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return first-user, login, and current-user recovery responses."""

        requests.append(request)
        if request.url.path.endswith("/users/first"):
            return httpx.Response(httpx.codes.OK, json={})
        if request.url.path.endswith("/users/login"):
            return httpx.Response(httpx.codes.CREATED, json={"session_token": "ephemeral"})
        return httpx.Response(
            httpx.codes.OK,
            json={"username": ADMIN_USERNAME, "email": ADMIN_EMAIL},
        )

    with CoderClient(
        "https://coder.example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.has_first_user() is True
        client.verify_prepared_first_user(PASSWORD)

    assert requests[2].headers["coder-session-token"] == "ephemeral"
    assert "coder-session-token" not in requests[0].headers


def test_delete_user_accounts_authenticates_encodes_and_accepts_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete a batch with one session and accept an already absent account."""

    requests: list[httpx.Request] = []
    heartbeats: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return successful authentication and idempotent deletion responses."""

        requests.append(request)
        if request.url.path.endswith("/users/first"):
            return httpx.Response(httpx.codes.OK, json={})
        if request.url.path.endswith("/users/login"):
            return httpx.Response(httpx.codes.CREATED, json={"session_token": "ephemeral"})
        if request.url.path.endswith("/users/me"):
            return httpx.Response(
                httpx.codes.OK,
                json={"username": ADMIN_USERNAME, "email": ADMIN_EMAIL},
            )
        if request.url.raw_path.endswith(b"/already-missing"):
            return httpx.Response(httpx.codes.NOT_FOUND, text="private missing response")
        return httpx.Response(httpx.codes.OK, json={"message": "deleted"})

    original_client = coder_client.CoderClient

    def client_factory(instance_url: str) -> CoderClient:
        """Create the service client with a deterministic mock transport."""

        return original_client(instance_url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("coder_manager.domains.coder.service.CoderClient", client_factory)
    delete_user_accounts(
        "https://coder.example.test/root",
        PASSWORD,
        ("alice/example", "already-missing"),
        heartbeat=lambda: heartbeats.append("heartbeat"),
    )

    assert [request.method for request in requests] == ["GET", "POST", "GET", "DELETE", "DELETE"]
    assert requests[3].url.raw_path.endswith(b"/api/v2/users/alice%2Fexample")
    assert all(request.headers["coder-session-token"] == "ephemeral" for request in requests[3:])
    assert heartbeats == ["heartbeat", "heartbeat"]


def test_delete_user_rejects_remote_errors_without_response_body() -> None:
    """Raise a sanitized error when Coder refuses account deletion."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return the workspace ownership conflict emitted by Coder."""

        return httpx.Response(httpx.codes.EXPECTATION_FAILED, text="private workspace details")

    with (
        CoderClient(
            "https://coder.example.test",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(CoderRequestError) as raised,
    ):
        client.delete_user("alice")

    assert str(raised.value) == "Coder DELETE api/v2/users/alice returned HTTP 417"
    assert "private workspace details" not in str(raised.value)


def test_delete_user_accounts_bootstraps_a_missing_administrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create and authenticate the prepared administrator before deletion."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return the empty deployment bootstrap and deletion sequence."""

        requests.append(request)
        if request.url.path.endswith("/users/first") and request.method == "GET":
            return httpx.Response(
                httpx.codes.NOT_FOUND,
                headers={"X-Coder-Build-Version": "v2.35.2"},
            )
        if request.url.path.endswith("/users/first"):
            return httpx.Response(httpx.codes.CREATED, json={"user_id": "ignored"})
        if request.url.path.endswith("/users/login"):
            return httpx.Response(httpx.codes.CREATED, json={"session_token": "ephemeral"})
        if request.url.path.endswith("/users/me"):
            return httpx.Response(
                httpx.codes.OK,
                json={"username": ADMIN_USERNAME, "email": ADMIN_EMAIL},
            )
        return httpx.Response(httpx.codes.OK, json={"message": "deleted"})

    original_client = coder_client.CoderClient
    monkeypatch.setattr(
        "coder_manager.domains.coder.service.CoderClient",
        lambda instance_url: original_client(
            instance_url,
            transport=httpx.MockTransport(handler),
        ),
    )

    delete_user_accounts("https://coder.example.test", PASSWORD, ("alice",))

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/api/v2/users/first"),
        ("POST", "/api/v2/users/first"),
        ("POST", "/api/v2/users/login"),
        ("GET", "/api/v2/users/me"),
        ("DELETE", "/api/v2/users/alice"),
    ]


def test_cleanup_user_accounts_deletes_every_unreferenced_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """List all Coder users and delete only accounts absent from the manager set."""

    requests: list[httpx.Request] = []
    heartbeats: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return authentication, the complete user page, and deletions."""

        requests.append(request)
        if request.url.path.endswith("/users/first"):
            return httpx.Response(httpx.codes.OK, json={})
        if request.url.path.endswith("/users/login"):
            return httpx.Response(httpx.codes.CREATED, json={"session_token": "ephemeral"})
        if request.url.path.endswith("/users/me"):
            return httpx.Response(
                httpx.codes.OK,
                json={"username": ADMIN_USERNAME, "email": ADMIN_EMAIL},
            )
        if request.url.path.endswith("/users") and request.method == "GET":
            return httpx.Response(
                httpx.codes.OK,
                json={
                    "count": 4,
                    "users": [
                        {"username": "service-account"},
                        {"username": "alice"},
                        {"username": "orphan"},
                        {"username": "admin"},
                    ],
                },
            )
        return httpx.Response(httpx.codes.OK, json={"message": "deleted"})

    original_client = coder_client.CoderClient
    monkeypatch.setattr(
        "coder_manager.domains.coder.service.CoderClient",
        lambda instance_url: original_client(
            instance_url,
            transport=httpx.MockTransport(handler),
        ),
    )

    deleted = cleanup_user_accounts(
        "https://coder.example.test",
        PASSWORD,
        ("admin", "alice"),
        heartbeat=lambda: heartbeats.append("heartbeat"),
    )

    assert deleted == ("orphan", "service-account")
    delete_paths = [request.url.path for request in requests if request.method == "DELETE"]
    assert delete_paths == [
        "/api/v2/users/orphan",
        "/api/v2/users/service-account",
    ]
    assert heartbeats == ["heartbeat", "heartbeat", "heartbeat"]


@pytest.mark.parametrize(
    "payload",
    [
        {"count": 2, "users": [{"username": "alice"}]},
        {"count": 1, "users": [{"id": "missing-username"}]},
        {"count": 2, "users": [{"username": "alice"}, {"username": "alice"}]},
    ],
)
def test_usernames_rejects_incomplete_or_invalid_pages(payload: object) -> None:
    """Fail closed when Coder does not return one complete unique user page."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return the invalid page under test."""

        return httpx.Response(httpx.codes.OK, json=payload)

    with (
        CoderClient(
            "https://coder.example.test",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(CoderRequestError, match="incomplete users page"),
    ):
        client.usernames()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(httpx.codes.NOT_FOUND),
        httpx.Response(httpx.codes.INTERNAL_SERVER_ERROR, text="private response"),
    ],
)
def test_first_user_detection_rejects_unverified_or_failed_responses(
    response: httpx.Response,
) -> None:
    """Reject non-Coder 404s and sanitize remote server errors."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return the parameterized invalid response."""

        return response

    with (
        CoderClient(
            "https://coder.example.test",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(CoderRequestError) as raised,
    ):
        client.has_first_user()

    message = str(raised.value)
    assert "private response" not in message
    assert PASSWORD.get_secret_value() not in message


@pytest.mark.parametrize(
    ("login_response", "me_response"),
    [
        (httpx.Response(httpx.codes.UNAUTHORIZED), None),
        (httpx.Response(httpx.codes.CREATED, json=[]), None),
        (httpx.Response(httpx.codes.CREATED, json={}), None),
        (
            httpx.Response(httpx.codes.CREATED, json={"session_token": "ephemeral"}),
            httpx.Response(
                httpx.codes.OK,
                json={"username": "someone-else", "email": ADMIN_EMAIL},
            ),
        ),
    ],
)
def test_recovery_rejects_foreign_or_invalid_users(
    login_response: httpx.Response,
    me_response: httpx.Response | None,
) -> None:
    """Fail safely when the existing first user cannot be verified."""

    responses = iter((login_response,) if me_response is None else (login_response, me_response))

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return the next recovery response."""

        return next(responses)

    expected_error = (
        CoderFirstUserConflictError
        if login_response.status_code == httpx.codes.UNAUTHORIZED or me_response is not None
        else CoderRequestError
    )
    with (
        CoderClient(
            "https://coder.example.test",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(expected_error),
    ):
        client.verify_prepared_first_user(PASSWORD)


def test_template_creation_http_contract() -> None:
    """Authenticate, upload, import, and create a first remote template."""

    organization_id = UUID("10000000-0000-0000-0000-000000000001")
    file_id = UUID("20000000-0000-0000-0000-000000000002")
    version_id = UUID("30000000-0000-0000-0000-000000000003")
    template_id = UUID("40000000-0000-0000-0000-000000000004")
    requests: list[httpx.Request] = []
    version_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        """Return the exact Coder API responses required by a first push."""

        nonlocal version_reads
        requests.append(request)
        path = request.url.path
        if path.endswith("/users/login"):
            return httpx.Response(201, json={"session_token": "ephemeral"})
        if path.endswith("/users/me"):
            return httpx.Response(200, json={"username": ADMIN_USERNAME, "email": ADMIN_EMAIL})
        if path.endswith("/organizations"):
            return httpx.Response(200, json=[{"id": str(organization_id), "is_default": True}])
        if path.endswith("/templates/python"):
            return httpx.Response(404)
        if path.endswith("/files"):
            return httpx.Response(201, json={"hash": str(file_id)})
        if path.endswith("/templateversions") and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "id": str(version_id),
                    "archived": False,
                    "job": {"status": "pending"},
                },
            )
        if path.endswith(f"/templateversions/{version_id}"):
            version_reads += 1
            return httpx.Response(
                200,
                json={
                    "id": str(version_id),
                    "archived": False,
                    "job": {"status": "succeeded" if version_reads > 1 else "running"},
                },
            )
        if path.endswith(f"/organizations/{organization_id}/templates"):
            return httpx.Response(200, json={"id": str(template_id)})
        message = f"Unexpected request: {request.method} {path}"
        raise AssertionError(message)

    with CoderClient(
        "https://coder.example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.authenticate_prepared_admin(PASSWORD)
        assert client.default_organization_id() == organization_id
        assert client.template_by_name(organization_id, "python") is None
        assert client.upload_template_archive(b"ustar") == file_id
        version = client.create_template_version(
            organization_id,
            file_id=file_id,
            version_name="git-" + ("a" * 40),
            template_id=None,
        )
        assert version.status == "pending"
        waited = client.wait_template_version(
            version.id,
            timeout_seconds=5,
            poll_interval_seconds=0.001,
        )
        assert waited.status == "succeeded"
        created = client.create_template(
            organization_id,
            name="python",
            display_name="Python",
            version_id=waited.id,
        )
        assert created.id == template_id

    authenticated = requests[2:]
    assert all(request.headers["coder-session-token"] == "ephemeral" for request in authenticated)
    upload = next(request for request in requests if request.url.path.endswith("/files"))
    assert upload.headers["content-type"] == "application/x-tar"
    assert upload.content == b"ustar"


def test_existing_archived_template_version_can_be_reactivated() -> None:
    """Read, unarchive, and activate an existing deterministic version."""

    organization_id = UUID("10000000-0000-0000-0000-000000000001")
    template_id = UUID("40000000-0000-0000-0000-000000000004")
    version_id = UUID("30000000-0000-0000-0000-000000000003")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return an adopted template and its archived successful version."""

        requests.append(request)
        path = request.url.path
        if path.endswith("/templates/python"):
            return httpx.Response(200, json={"id": str(template_id)})
        if "/versions/git-" in path and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": str(version_id),
                    "archived": True,
                    "job": {"status": "succeeded"},
                },
            )
        return httpx.Response(200, json={})

    with CoderClient(
        "https://coder.example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        remote = client.template_by_name(organization_id, "python")
        assert remote is not None
        version = client.template_version_by_name(
            organization_id,
            "python",
            "git-" + ("a" * 40),
        )
        assert version is not None
        client.unarchive_template_version(version.id)
        client.activate_template_version(remote.id, version.id)

    assert [request.method for request in requests] == ["GET", "GET", "POST", "PATCH"]
