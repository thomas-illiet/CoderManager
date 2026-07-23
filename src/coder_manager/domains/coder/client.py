"""Synchronous HTTP client for the unauthenticated Coder bootstrap API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

import httpx

from coder_manager.domains.coder.constants import (
    ADMIN_EMAIL,
    ADMIN_NAME,
    ADMIN_USERNAME,
)
from coder_manager.domains.coder.errors import (
    CoderFirstUserConflictError,
    CoderRequestError,
)

if TYPE_CHECKING:
    from types import TracebackType

    from pydantic import SecretStr

BUILD_VERSION_HEADER = "X-Coder-Build-Version"
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 30.0


class CoderClient:
    """Small synchronous client for first-user creation and recovery."""

    def __init__(
        self,
        instance_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Create a strict-TLS client with bounded connection and read timeouts."""

        self._client = httpx.Client(
            base_url=f"{instance_url.rstrip('/')}/",
            timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self) -> Self:
        """Return this client when entering a managed context."""

        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Close the underlying HTTP connection pool."""

        self.close()

    def close(self) -> None:
        """Close the reusable HTTP connection pool."""

        self._client.close()

    def has_first_user(self) -> bool:
        """Return whether Coder already has a first user."""

        path = "api/v2/users/first"
        response = self._client.get(path)
        if response.status_code == httpx.codes.OK:
            return True
        if response.status_code == httpx.codes.NOT_FOUND:
            if not response.headers.get(BUILD_VERSION_HEADER):
                msg = "Coder GET api/v2/users/first returned an unverified HTTP 404"
                raise CoderRequestError(msg)
            return False
        self._raise_for_response(response, "GET", path, httpx.codes.OK)
        return False  # pragma: no cover - _raise_for_response always raises

    def create_first_user(self, password: SecretStr) -> None:
        """Create the static administrator as Coder's first user."""

        path = "api/v2/users/first"
        response = self._client.post(
            path,
            json={
                "email": ADMIN_EMAIL,
                "username": ADMIN_USERNAME,
                "name": ADMIN_NAME,
                "password": password.get_secret_value(),
                "trial": False,
            },
        )
        self._raise_for_response(response, "POST", path, httpx.codes.CREATED)

    def verify_prepared_first_user(self, password: SecretStr) -> None:
        """Authenticate prepared credentials and require the static identity."""

        login_path = "api/v2/users/login"
        response = self._client.post(
            login_path,
            json={
                "email": ADMIN_EMAIL,
                "password": password.get_secret_value(),
            },
        )
        if response.status_code in {httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN}:
            msg = "Coder already has a first user that does not match prepared credentials"
            raise CoderFirstUserConflictError(msg)
        self._raise_for_response(response, "POST", login_path, httpx.codes.CREATED)
        payload = self._json_object(response, login_path)
        token = payload.get("session_token")
        if not isinstance(token, str) or not token:
            msg = "Coder POST api/v2/users/login returned an invalid session token"
            raise CoderRequestError(msg)

        me_path = "api/v2/users/me"
        response = self._client.get(
            me_path,
            headers={"Coder-Session-Token": token},
        )
        self._raise_for_response(response, "GET", me_path, httpx.codes.OK)
        user = self._json_object(response, me_path)
        if user.get("username") != ADMIN_USERNAME or user.get("email") != ADMIN_EMAIL:
            msg = "Coder already has a first user that does not match prepared credentials"
            raise CoderFirstUserConflictError(msg)

    @staticmethod
    def _raise_for_response(
        response: httpx.Response,
        method: str,
        path: str,
        expected_status: int,
    ) -> None:
        """Raise a sanitized error without including response bodies."""

        if response.status_code == expected_status:
            return
        msg = f"Coder {method} {path} returned HTTP {response.status_code}"
        raise CoderRequestError(msg)

    @staticmethod
    def _json_object(response: httpx.Response, path: str) -> dict[str, Any]:
        """Decode a JSON response while requiring an object payload."""

        try:
            payload = response.json()
        except ValueError as error:
            msg = f"Coder {path} returned invalid JSON"
            raise CoderRequestError(msg) from error
        if not isinstance(payload, dict):
            msg = f"Coder {path} returned non-object JSON"
            raise CoderRequestError(msg)
        return payload
