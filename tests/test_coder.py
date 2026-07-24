"""Coder first-user bootstrap HTTP contract tests."""

import json
from uuid import UUID

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
)

PASSWORD = SecretStr("prepared-secret-password")


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
