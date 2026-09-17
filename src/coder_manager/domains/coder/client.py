"""Synchronous HTTP client for managed Coder instance workflows."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import quote
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
from coder_manager.domains.coder.models import (
    CoderTemplate,
    CoderTemplateVersion,
    CoderWorkspace,
    CoderWorkspaceBuild,
    CoderWorkspacePage,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from pydantic import SecretStr

BUILD_VERSION_HEADER = "X-Coder-Build-Version"
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 30.0
TEMPLATE_IMPORT_ERROR_MAX_CHARS = 4096
TEMPLATE_IMPORT_LOG_MAX_CHARS = 8192
TEMPLATE_IMPORT_LOG_MAX_ENTRIES = 50
logger = logging.getLogger(__name__)
WORKSPACE_STATUSES = frozenset(
    {
        "pending",
        "starting",
        "running",
        "stopping",
        "stopped",
        "failed",
        "canceling",
        "canceled",
        "deleting",
        "deleted",
    }
)
WORKSPACE_TRANSITIONS = frozenset({"start", "stop", "delete"})


class CoderClient:
    """Small synchronous client for administrator-driven Coder API workflows."""

    def __init__(
        self,
        instance_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Create a client with disabled TLS verification and bounded timeouts."""

        self._client = httpx.Client(
            base_url=f"{instance_url.rstrip('/')}/",
            timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
            verify=False,  # noqa: S501 - managed instances use private certificates.
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

    def delete_user(self, username: str) -> None:
        """Delete one user account, treating an absent account as converged."""

        path = f"api/v2/users/{quote(username, safe='')}"
        response = self._client.delete(path)
        if response.status_code == httpx.codes.NOT_FOUND:
            return
        self._raise_for_response(response, "DELETE", path, httpx.codes.OK)

    def usernames(self) -> tuple[str, ...]:
        """Return every non-deleted Coder username from the unpaginated endpoint."""

        path = "api/v2/users"
        response = self._client.get(path)
        self._raise_for_response(response, "GET", path, httpx.codes.OK)
        payload = self._json_object(response, path)
        users = payload.get("users")
        count = payload.get("count")
        if not isinstance(users, list) or type(count) is not int:
            msg = "Coder GET api/v2/users returned an invalid users page"
            raise CoderRequestError(msg)
        usernames = tuple(
            user.get("username")
            for user in users
            if isinstance(user, dict) and isinstance(user.get("username"), str)
        )
        if (
            len(usernames) != len(users)
            or len(usernames) != count
            or any(not username for username in usernames)
            or len(set(usernames)) != len(usernames)
        ):
            msg = "Coder GET api/v2/users returned an incomplete users page"
            raise CoderRequestError(msg)
        return usernames

    def workspaces(
        self,
        *,
        status: str | None,
        offset: int,
        limit: int,
    ) -> CoderWorkspacePage:
        """Return one validated page of non-deleted workspaces."""

        path = "api/v2/workspaces"
        response = self._client.get(
            path,
            params={
                "q": "" if status is None else f'status:"{status}"',
                "offset": offset,
                "limit": limit,
            },
        )
        self._raise_for_response(response, "GET", path, httpx.codes.OK)
        payload = self._json_object(response, path)
        raw_workspaces = payload.get("workspaces")
        count = payload.get("count")
        if not isinstance(raw_workspaces, list) or type(count) is not int or count < 0:
            msg = "Coder GET api/v2/workspaces returned an invalid workspace page"
            raise CoderRequestError(msg)
        workspaces: list[CoderWorkspace] = []
        for item in raw_workspaces:
            if not isinstance(item, dict):
                msg = "Coder GET api/v2/workspaces returned an invalid workspace"
                raise CoderRequestError(msg)
            latest_build = item.get("latest_build")
            if not isinstance(latest_build, dict):
                msg = "Coder GET api/v2/workspaces returned an invalid latest build"
                raise CoderRequestError(msg)
            build_status = latest_build.get("status")
            transition = latest_build.get("transition")
            if (
                not isinstance(build_status, str)
                or build_status not in WORKSPACE_STATUSES
                or not isinstance(transition, str)
                or transition not in WORKSPACE_TRANSITIONS
            ):
                msg = "Coder GET api/v2/workspaces returned an invalid latest build"
                raise CoderRequestError(msg)
            workspaces.append(
                CoderWorkspace(
                    id=self._uuid_field(item, "id", path),
                    status=build_status,
                    latest_build_id=self._uuid_field(latest_build, "id", path),
                    latest_build_transition=transition,
                    name=item.get("name") if isinstance(item.get("name"), str) else "",
                    template_id=self._optional_uuid_field(item, "template_id", path),
                )
            )
        expected_items = min(limit, max(count - offset, 0))
        if len(workspaces) != expected_items:
            msg = "Coder GET api/v2/workspaces returned an incomplete workspace page"
            raise CoderRequestError(msg)
        return CoderWorkspacePage(items=tuple(workspaces), count=count)

    def workspace(self, workspace_id: UUID) -> CoderWorkspace | None:
        """Return one remote workspace by ID, treating absence as convergence."""

        path = f"api/v2/workspaces/{workspace_id}"
        response = self._client.get(path)
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        self._raise_for_response(response, "GET", path, httpx.codes.OK)
        return self._workspace(response, path)

    def workspace_by_owner_and_name(
        self,
        username: str,
        name: str,
    ) -> CoderWorkspace | None:
        """Find one workspace by its owner and stable name."""

        path = f"api/v2/users/{quote(username, safe='')}/workspace/{quote(name, safe='')}"
        response = self._client.get(path)
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        self._raise_for_response(response, "GET", path, httpx.codes.OK)
        return self._workspace(response, path)

    def create_workspace(
        self,
        username: str,
        *,
        name: str,
        template_id: UUID,
        rich_parameter_values: tuple[tuple[str, str], ...],
    ) -> CoderWorkspace:
        """Create one user workspace from a remote template."""

        path = f"api/v2/users/{quote(username, safe='')}/workspaces"
        response = self._client.post(
            path,
            json={
                "name": name,
                "template_id": str(template_id),
                "rich_parameter_values": [
                    {"name": parameter_name, "value": value}
                    for parameter_name, value in rich_parameter_values
                ],
            },
        )
        self._raise_for_response(response, "POST", path, httpx.codes.CREATED)
        return self._workspace(response, path)

    def update_workspace_name(self, workspace_id: UUID, name: str) -> None:
        """Replace one remote workspace's mutable name."""

        path = f"api/v2/workspaces/{workspace_id}"
        response = self._client.patch(path, json={"name": name})
        self._raise_for_response(response, "PATCH", path, httpx.codes.NO_CONTENT)

    def create_workspace_start_build(
        self,
        workspace_id: UUID,
        rich_parameter_values: tuple[tuple[str, str], ...],
    ) -> CoderWorkspaceBuild:
        """Start one workspace using the supplied rich parameter snapshot."""

        path = f"api/v2/workspaces/{workspace_id}/builds"
        response = self._client.post(
            path,
            json={
                "transition": "start",
                "rich_parameter_values": [
                    {"name": parameter_name, "value": value}
                    for parameter_name, value in rich_parameter_values
                ],
            },
        )
        self._raise_for_response(response, "POST", path, httpx.codes.CREATED)
        return self._workspace_build(response, path)

    def workspace_build_parameters(self, build_id: UUID) -> tuple[tuple[str, str], ...]:
        """Return one build's validated rich parameter snapshot."""

        path = f"api/v2/workspacebuilds/{build_id}/parameters"
        response = self._client.get(path)
        self._raise_for_response(response, "GET", path, httpx.codes.OK)
        payload = self._json_array(response, path)
        values: list[tuple[str, str]] = []
        for item in payload:
            name = item.get("name")
            value = item.get("value")
            if not isinstance(name, str) or not name or not isinstance(value, str):
                msg = f"Coder {path} returned invalid build parameters"
                raise CoderRequestError(msg)
            values.append((name, value))
        if len({name for name, _value in values}) != len(values):
            msg = f"Coder {path} returned duplicate build parameters"
            raise CoderRequestError(msg)
        return tuple(sorted(values))

    def create_workspace_stop_build(self, workspace_id: UUID) -> CoderWorkspaceBuild:
        """Queue a stop build for one remote workspace."""

        path = f"api/v2/workspaces/{workspace_id}/builds"
        response = self._client.post(path, json={"transition": "stop"})
        self._raise_for_response(response, "POST", path, httpx.codes.CREATED)
        return self._workspace_build(response, path)

    def create_workspace_delete_build(self, workspace_id: UUID) -> CoderWorkspaceBuild:
        """Queue a non-orphan delete build for one remote workspace."""

        path = f"api/v2/workspaces/{workspace_id}/builds"
        response = self._client.post(path, json={"transition": "delete"})
        self._raise_for_response(response, "POST", path, httpx.codes.CREATED)
        return self._workspace_build(response, path)

    def workspace_build(self, build_id: UUID) -> CoderWorkspaceBuild:
        """Read one remote workspace build."""

        path = f"api/v2/workspacebuilds/{build_id}"
        response = self._client.get(path)
        self._raise_for_response(response, "GET", path, httpx.codes.OK)
        return self._workspace_build(response, path)

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
        """Find a remote template by its stable technical name."""

        path = f"api/v2/organizations/{organization_id}/templates/{name}"
        response = self._client.get(path)
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        self._raise_for_response(response, "GET", path, httpx.codes.OK)
        return self._template(response, path)

    def template(self, template_id: UUID) -> CoderTemplate | None:
        """Find a remote template by persisted ID for retry recovery."""

        path = f"api/v2/templates/{template_id}"
        response = self._client.get(path)
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        self._raise_for_response(response, "GET", path, httpx.codes.OK)
        return self._template(response, path)

    def delete_template(self, template_id: UUID) -> None:
        """Delete one template, treating an absent remote template as converged."""

        path = f"api/v2/templates/{template_id}"
        response = self._client.delete(path)
        if response.status_code == httpx.codes.NOT_FOUND:
            return
        self._raise_for_response(response, "DELETE", path, httpx.codes.OK)

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

    def template_version_for_recovery(
        self,
        version_id: UUID,
    ) -> CoderTemplateVersion | None:
        """Read a persisted retry version, treating external deletion as absence."""

        path = f"api/v2/templateversions/{version_id}"
        response = self._client.get(path)
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
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
        user_variable_values: tuple[tuple[str, str], ...] = (),
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
            "user_variable_values": [
                {"name": name, "value": value} for name, value in user_variable_values
            ],
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
        return self._template(response, path)

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
        sensitive_values: tuple[str, ...] = (),
    ) -> CoderTemplateVersion:
        """Poll a provisioner import until success, terminal failure, or timeout."""

        deadline = time.monotonic() + timeout_seconds
        while True:
            version = self.template_version(version_id)
            if version.status == "succeeded":
                return version
            if version.status in {"failed", "canceled", "cancelled"}:
                details = [
                    "Coder template import failed",
                    f"version_id={version.id}",
                    f"status={version.status}",
                ]
                if version.job_id is not None:
                    details.append(f"job_id={version.job_id}")
                if version.error_code:
                    details.append(f"error_code={version.error_code}")
                if version.error:
                    details.append(
                        "reason=" + self._sanitize_diagnostic(version.error, sensitive_values)[0]
                    )
                self._log_template_import_failure(version, sensitive_values)
                msg = ": ".join((details[0], ", ".join(details[1:])))
                raise CoderRequestError(msg)
            if time.monotonic() >= deadline:
                msg = "Coder template import timed out"
                raise CoderRequestError(msg)
            if heartbeat is not None:
                heartbeat()
            time.sleep(poll_interval_seconds)

    def _log_template_import_failure(
        self,
        version: CoderTemplateVersion,
        sensitive_values: tuple[str, ...],
    ) -> None:
        """Log a bounded, redacted provisioner excerpt without hiding the primary error."""

        try:
            lines, truncated = self.template_version_logs(
                version.id,
                sensitive_values=sensitive_values,
            )
        except (CoderRequestError, httpx.HTTPError) as error:
            logger.warning(
                "Could not retrieve Coder template import logs for version %s: %s",
                version.id,
                error,
            )
            return
        logger.error(
            "Coder template import diagnostics version_id=%s job_id=%s error_code=%s "
            "logs_overflowed=%s logs_truncated=%s\n%s",
            version.id,
            version.job_id,
            version.error_code,
            version.logs_overflowed,
            truncated,
            "\n".join(lines) if lines else "<no provisioner logs returned>",
        )

    def template_version_logs(
        self,
        version_id: UUID,
        *,
        sensitive_values: tuple[str, ...] = (),
    ) -> tuple[tuple[str, ...], bool]:
        """Return a bounded, redacted tail of one template import's provisioner logs."""

        path = f"api/v2/templateversions/{version_id}/logs"
        response = self._client.get(path)
        self._raise_for_response(response, "GET", path, httpx.codes.OK)
        payload = self._json_array(response, path)
        rendered: list[str] = []
        for entry in payload[-TEMPLATE_IMPORT_LOG_MAX_ENTRIES:]:
            level = entry.get("log_level")
            stage = entry.get("stage")
            output = entry.get("output")
            if not all(isinstance(value, str) for value in (level, stage, output)):
                continue
            line, _ = self._sanitize_diagnostic(
                f"[{stage}] [{level}] {output}",
                sensitive_values,
                max_chars=TEMPLATE_IMPORT_LOG_MAX_CHARS,
            )
            rendered.append(line)

        truncated = len(payload) > TEMPLATE_IMPORT_LOG_MAX_ENTRIES
        while sum(len(line) + 1 for line in rendered) > TEMPLATE_IMPORT_LOG_MAX_CHARS:
            rendered.pop(0)
            truncated = True
        return tuple(rendered), truncated

    @staticmethod
    def _sanitize_diagnostic(
        value: str,
        sensitive_values: tuple[str, ...],
        *,
        max_chars: int = TEMPLATE_IMPORT_ERROR_MAX_CHARS,
    ) -> tuple[str, bool]:
        """Redact known values, normalize whitespace, and bound remote diagnostics."""

        sanitized = " ".join(value.split())
        for sensitive in sorted(filter(None, sensitive_values), key=len, reverse=True):
            sanitized = sanitized.replace(sensitive, "<redacted>")
        truncated = len(sanitized) > max_chars
        if truncated:
            sanitized = sanitized[: max_chars - 3] + "..."
        return sanitized, truncated

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

    @staticmethod
    def _optional_uuid_field(
        payload: dict[str, Any],
        field: str,
        path: str,
    ) -> UUID | None:
        """Decode one optional UUID while rejecting malformed present values."""

        if field not in payload or payload[field] is None:
            return None
        try:
            return UUID(str(payload[field]))
        except (TypeError, ValueError) as error:
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
            job_id=cls._optional_uuid_field(job, "id", path),
            error=job.get("error") if isinstance(job.get("error"), str) else None,
            error_code=(job.get("error_code") if isinstance(job.get("error_code"), str) else None),
            logs_overflowed=job.get("logs_overflowed") is True,
        )

    @classmethod
    def _template(
        cls,
        response: httpx.Response,
        path: str,
    ) -> CoderTemplate:
        """Decode the stable and active identities required for safe recovery."""

        payload = cls._json_object(response, path)
        return CoderTemplate(
            id=cls._uuid_field(payload, "id", path),
            active_version_id=cls._optional_uuid_field(payload, "active_version_id", path),
        )

    @classmethod
    def _workspace_build(
        cls,
        response: httpx.Response,
        path: str,
    ) -> CoderWorkspaceBuild:
        """Decode the fields required to wait for a workspace build."""

        payload = cls._json_object(response, path)
        status = payload.get("status")
        transition = payload.get("transition")
        if not isinstance(status, str) or status not in WORKSPACE_STATUSES:
            msg = f"Coder {path} returned an invalid workspace build status"
            raise CoderRequestError(msg)
        if not isinstance(transition, str) or transition not in WORKSPACE_TRANSITIONS:
            msg = f"Coder {path} returned an invalid workspace build transition"
            raise CoderRequestError(msg)
        return CoderWorkspaceBuild(
            id=cls._uuid_field(payload, "id", path),
            status=status,
            transition=transition,
        )

    @classmethod
    def _workspace(
        cls,
        response: httpx.Response,
        path: str,
    ) -> CoderWorkspace:
        """Decode the workspace identity and latest build required by workers."""

        payload = cls._json_object(response, path)
        name = payload.get("name")
        latest_build = payload.get("latest_build")
        if not isinstance(name, str) or not name or not isinstance(latest_build, dict):
            msg = f"Coder {path} returned an invalid workspace"
            raise CoderRequestError(msg)
        status = latest_build.get("status")
        transition = latest_build.get("transition")
        if (
            not isinstance(status, str)
            or status not in WORKSPACE_STATUSES
            or not isinstance(transition, str)
            or transition not in WORKSPACE_TRANSITIONS
        ):
            msg = f"Coder {path} returned an invalid latest build"
            raise CoderRequestError(msg)
        return CoderWorkspace(
            id=cls._uuid_field(payload, "id", path),
            status=status,
            latest_build_id=cls._uuid_field(latest_build, "id", path),
            latest_build_transition=transition,
            name=name,
            template_id=cls._uuid_field(payload, "template_id", path),
        )
