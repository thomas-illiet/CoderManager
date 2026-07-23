"""Coder first-user bootstrap HTTP contract tests."""

import json

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
