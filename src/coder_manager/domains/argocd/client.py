"""Synchronous HTTP client for the Argo CD Applications API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

import httpx

from coder_manager.domains.argocd.applications import (
    application_name,
    application_payload,
    application_status,
    application_update_payload,
)
from coder_manager.domains.argocd.errors import (
    ArgoCdApplicationNotFoundError,
    ArgoCdRequestError,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from types import TracebackType
    from uuid import UUID

    from coder_manager.domains.argocd.config import ArgoCdConfig
    from coder_manager.domains.argocd.models import ArgoCdApplicationStatus

HTTP_SUCCESS_MIN = 200
HTTP_SUCCESS_MAX = 300
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 30.0


class ArgoCdClient:
    """Small synchronous client for the Argo CD Applications API."""

    def __init__(
        self,
        config: ArgoCdConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Create a configured HTTP client with bounded connection and read timeouts."""

        self._config = config
        self._client = httpx.Client(
            base_url=f"{config.url}/",
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
            verify=not config.skip_ssl_verify,
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
        """Close the underlying HTTP client when leaving a managed context."""

        self.close()

    def close(self) -> None:
        """Close the reusable HTTP connection pool."""

        self._client.close()

    def ensure_application(
        self,
        instance_id: UUID,
        attached_name: str | None,
        members: Iterable[tuple[str, str]],
    ) -> str:
        """Create or overwrite an Application and request one synchronization."""

        name = application_name(self._config, instance_id, attached_name)
        desired = application_payload(self._config, name, instance_id, members)
        existing = self._get_application(name)

        # Attempt creation first, but recover if another worker won the race.
        if existing is None:
            response = self._client.post(
                "api/v1/applications",
                params={"upsert": "false", "validate": "true"},
                json=desired,
            )
            if response.status_code == httpx.codes.CONFLICT:
                existing = self._get_application(name)
                if existing is None:
                    self._raise_for_response(response, "POST", "api/v1/applications")
            else:
                self._raise_for_response(response, "POST", "api/v1/applications")

        # Replace only desired fields while retaining metadata owned by Argo CD.
        if existing is not None:
            path = f"api/v1/applications/{name}"
            response = self._client.put(
                path,
                params={"project": self._config.project, "validate": "true"},
                json=application_update_payload(existing, desired),
            )
            self._raise_for_response(response, "PUT", path)

        # Explicitly synchronize after either creation or update.
        sync_path = f"api/v1/applications/{name}/sync"
        response = self._client.post(
            sync_path,
            params={"project": self._config.project},
            json={},
        )
        self._raise_for_response(response, "POST", sync_path)
        return name

    def get_application_status(
        self,
        instance_id: UUID,
        attached_name: str | None,
    ) -> ArgoCdApplicationStatus:
        """Return a sanitized snapshot of an Application's remote status."""

        name = application_name(self._config, instance_id, attached_name)
        application = self._get_application(name)
        if application is None:
            raise ArgoCdApplicationNotFoundError(name)
        return application_status(name, application)

    def _get_application(self, name: str) -> dict[str, Any] | None:
        """Fetch one Application, returning none only for an explicit 404 response."""

        path = f"api/v1/applications/{name}"
        response = self._client.get(path, params={"project": self._config.project})
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        self._raise_for_response(response, "GET", path)
        return _json_object(response, path)

    @staticmethod
    def _raise_for_response(response: httpx.Response, method: str, path: str) -> None:
        """Raise a sanitized domain error for a non-successful HTTP response."""

        if HTTP_SUCCESS_MIN <= response.status_code < HTTP_SUCCESS_MAX:
            return
        msg = f"Argo CD {method} {path} returned HTTP {response.status_code}"
        raise ArgoCdRequestError(msg)


def _json_object(response: httpx.Response, path: str) -> dict[str, Any]:
    """Decode an HTTP response and require a JSON object payload."""

    try:
        payload = response.json()
    except ValueError as error:
        msg = f"Argo CD GET {path} returned invalid JSON"
        raise ArgoCdRequestError(msg) from error
    if not isinstance(payload, dict):
        msg = f"Argo CD GET {path} returned a non-object JSON response"
        raise ArgoCdRequestError(msg)
    return payload
