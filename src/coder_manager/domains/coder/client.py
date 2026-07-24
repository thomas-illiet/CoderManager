"""Synchronous HTTP client for the unauthenticated Coder bootstrap API."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID

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
from coder_manager.domains.coder.models import CoderTemplate, CoderTemplateVersion

if TYPE_CHECKING:
    from collections.abc import Callable
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

        self.authenticate_prepared_admin(password)

    def authenticate_prepared_admin(self, password: SecretStr) -> None:
        """Authenticate the prepared administrator for subsequent API calls."""

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
        self._client.headers["Coder-Session-Token"] = token

    def default_organization_id(self) -> UUID:
        """Return the single organization marked as the deployment default."""

        path = "api/v2/organizations"
        response = self._client.get(path)
        self._raise_for_response(response, "GET", path, httpx.codes.OK)
        payload = self._json_array(response, path)
        defaults = [item for item in payload if item.get("is_default") is True]
        if len(defaults) != 1:
            msg = "Coder did not return exactly one default organization"
            raise CoderRequestError(msg)
        return self._uuid_field(defaults[0], "id", path)

    def template_by_name(self, organization_id: UUID, name: str) -> CoderTemplate | None:
        """Find an adopted template by its stable technical name."""

        path = f"api/v2/organizations/{organization_id}/templates/{name}"
        response = self._client.get(path)
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        self._raise_for_response(response, "GET", path, httpx.codes.OK)
        payload = self._json_object(response, path)
        return CoderTemplate(id=self._uuid_field(payload, "id", path))

    def template_version_by_name(
        self,
        organization_id: UUID,
        template_name: str,
        version_name: str,
    ) -> CoderTemplateVersion | None:
        """Find one existing deterministic version attached to a template."""

        path = (
            f"api/v2/organizations/{organization_id}/templates/"
            f"{template_name}/versions/{version_name}"
        )
        response = self._client.get(path)
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        self._raise_for_response(response, "GET", path, httpx.codes.OK)
        return self._template_version(response, path)

    def template_version(self, version_id: UUID) -> CoderTemplateVersion:
        """Read one template version and its provisioner job state."""

        path = f"api/v2/templateversions/{version_id}"
        response = self._client.get(path)
        self._raise_for_response(response, "GET", path, httpx.codes.OK)
        return self._template_version(response, path)

    def upload_template_archive(self, content: bytes) -> UUID:
        """Upload one USTAR template archive and return Coder's file identifier."""

        path = "api/v2/files"
        response = self._client.post(
            path,
            content=content,
            headers={"Content-Type": "application/x-tar"},
        )
        if response.status_code not in {httpx.codes.OK, httpx.codes.CREATED}:
            self._raise_for_response(response, "POST", path, httpx.codes.CREATED)
        payload = self._json_object(response, path)
        return self._uuid_field(payload, "hash", path)

    def create_template_version(
        self,
        organization_id: UUID,
        *,
        file_id: UUID,
        version_name: str,
        template_id: UUID | None,
    ) -> CoderTemplateVersion:
        """Create one Terraform version from an uploaded archive."""

        path = f"api/v2/organizations/{organization_id}/templateversions"
        body: dict[str, Any] = {
            "file_id": str(file_id),
            "message": "Synchronized from CoderManager",
            "name": version_name,
            "provisioner": "terraform",
            "storage_method": "file",
            "tags": {},
            "user_variable_values": [],
        }
        if template_id is not None:
            body["template_id"] = str(template_id)
        response = self._client.post(path, json=body)
        self._raise_for_response(response, "POST", path, httpx.codes.CREATED)
        return self._template_version(response, path)

    def create_template(
        self,
        organization_id: UUID,
        *,
        name: str,
        display_name: str,
        version_id: UUID,
    ) -> CoderTemplate:
        """Create a Coder template from its first successful version."""

        path = f"api/v2/organizations/{organization_id}/templates"
        response = self._client.post(
            path,
            json={
                "name": name,
                "display_name": display_name,
                "template_version_id": str(version_id),
            },
        )
        self._raise_for_response(response, "POST", path, httpx.codes.OK)
        payload = self._json_object(response, path)
        return CoderTemplate(id=self._uuid_field(payload, "id", path))

    def activate_template_version(self, template_id: UUID, version_id: UUID) -> None:
        """Make one successful version active on an adopted template."""

        path = f"api/v2/templates/{template_id}/versions"
        response = self._client.patch(path, json={"id": str(version_id)})
        self._raise_for_response(response, "PATCH", path, httpx.codes.OK)

    def unarchive_template_version(self, version_id: UUID) -> None:
        """Restore an archived deterministic version before reactivation."""

        path = f"api/v2/templateversions/{version_id}/unarchive"
        response = self._client.post(path)
        self._raise_for_response(response, "POST", path, httpx.codes.OK)

    def wait_template_version(
        self,
        version_id: UUID,
        *,
        timeout_seconds: int,
        poll_interval_seconds: float,
        heartbeat: Callable[[], None] | None = None,
    ) -> CoderTemplateVersion:
        """Poll a provisioner import until success, terminal failure, or timeout."""

        deadline = time.monotonic() + timeout_seconds
        while True:
            version = self.template_version(version_id)
            if version.status == "succeeded":
                return version
            if version.status in {"failed", "canceled", "cancelled"}:
                msg = "Coder template import failed"
                raise CoderRequestError(msg)
            if time.monotonic() >= deadline:
                msg = "Coder template import timed out"
                raise CoderRequestError(msg)
            if heartbeat is not None:
                heartbeat()
            time.sleep(poll_interval_seconds)

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

    @staticmethod
    def _json_array(response: httpx.Response, path: str) -> list[dict[str, Any]]:
        """Decode a JSON array whose items must all be objects."""

        try:
            payload = response.json()
        except ValueError as error:
            msg = f"Coder {path} returned invalid JSON"
            raise CoderRequestError(msg) from error
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            msg = f"Coder {path} returned invalid array JSON"
            raise CoderRequestError(msg)
        return payload

    @staticmethod
    def _uuid_field(payload: dict[str, Any], field: str, path: str) -> UUID:
        """Decode one required UUID field from a remote response."""

        try:
            return UUID(str(payload[field]))
        except (KeyError, TypeError, ValueError) as error:
            msg = f"Coder {path} returned an invalid {field}"
            raise CoderRequestError(msg) from error

    @classmethod
    def _template_version(
        cls,
        response: httpx.Response,
        path: str,
    ) -> CoderTemplateVersion:
        """Decode the fields required to drive a template import."""

        payload = cls._json_object(response, path)
        job = payload.get("job")
        if not isinstance(job, dict) or not isinstance(job.get("status"), str):
            msg = f"Coder {path} returned an invalid provisioner job"
            raise CoderRequestError(msg)
        return CoderTemplateVersion(
            id=cls._uuid_field(payload, "id", path),
            status=job["status"],
            archived=payload.get("archived") is True,
        )
