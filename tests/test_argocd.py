"""Argo CD configuration and HTTP contract tests."""

import json
from uuid import UUID, uuid4

import httpx
import pytest

from coder_manager.config import Settings
from coder_manager.domains.argocd import (
    ArgoCdApplicationNotFoundError,
    ArgoCdClient,
    ArgoCdConfig,
    ArgoCdConfigurationError,
    ArgoCdRequestError,
)
from coder_manager.domains.argocd import client as argocd_client


def configured_settings(**overrides: object) -> Settings:
    """Build complete Argo CD settings with optional test overrides."""

    values: dict[str, object] = {
        "argocd_url": "https://argocd.test/root/",
        "argocd_token": "super-secret-token",
        "argocd_project": "coder",
        "argocd_application_prefix": "managed",
        "argocd_repository_url": "https://git.test/platform.git",
        "argocd_repository_path": "charts/coder",
        "argocd_target_revision": "v1.2.3",
        "argocd_destination_name": "in-cluster",
        "default_admins": " Root.Admin,alice ",
    }
    values.update(overrides)
    return Settings(**values)


def test_create_application_and_sync_contract() -> None:
    """Verify the create application and sync contract scenario."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Simulate the handler operation used by this scenario."""

        requests.append(request)
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(200, json={})

    config = ArgoCdConfig.from_settings(configured_settings())
    instance_id = UUID("12345678-1234-5678-1234-567812345678")
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        name = client.ensure_application(
            instance_id,
            None,
            (("zoe", "user"), ("alice", "admin")),
        )

    assert name == "managed-12345678123456781234567812345678"
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", f"/root/api/v1/applications/{name}"),
        ("POST", "/root/api/v1/applications"),
        ("POST", f"/root/api/v1/applications/{name}/sync"),
    ]
    assert all(
        request.headers["authorization"] == "Bearer super-secret-token" for request in requests
    )
    payload = json.loads(requests[1].content)
    assert payload["metadata"] == {
        "name": name,
        "labels": {
            "coder-manager/managed": "true",
            "coder-manager/instance-id": str(instance_id),
        },
    }
    assert payload["spec"]["source"] == {
        "repoURL": "https://git.test/platform.git",
        "path": "charts/coder",
        "targetRevision": "v1.2.3",
        "helm": {
            "releaseName": name,
            "parameters": [
                {
                    "name": "users",
                    "value": "alice,root.admin,zoe",
                    "forceString": True,
                },
                {
                    "name": "admins",
                    "value": "alice,root.admin",
                    "forceString": True,
                },
            ],
        },
    }
    assert payload["spec"]["destination"] == {
        "name": "in-cluster",
        "namespace": name,
    }
    assert payload["spec"]["syncPolicy"] == {
        "automated": {
            "prune": True,
            "selfHeal": True,
        }
    }


def test_existing_application_is_attached_and_overwritten() -> None:
    """Verify the existing application is attached and overwritten scenario."""

    requests: list[httpx.Request] = []
    attached_name = "legacy-attached"
    existing = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": attached_name,
            "resourceVersion": "42",
            "annotations": {"owner": "platform"},
            "labels": {"existing": "kept"},
        },
        "spec": {"project": "wrong"},
        "status": {"health": {"status": "Healthy"}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """Simulate the handler operation used by this scenario."""

        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=existing)
        return httpx.Response(200, json={})

    config = ArgoCdConfig.from_settings(configured_settings(default_admins=""))
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        returned_name = client.ensure_application(uuid4(), attached_name, ())

    assert returned_name == attached_name
    assert [request.method for request in requests] == ["GET", "PUT", "POST"]
    update = json.loads(requests[1].content)
    assert update["metadata"]["resourceVersion"] == "42"
    assert update["metadata"]["annotations"] == {"owner": "platform"}
    assert update["metadata"]["labels"]["existing"] == "kept"
    assert update["metadata"]["labels"]["coder-manager/managed"] == "true"
    assert update["spec"]["project"] == "coder"
    assert "status" not in update


def test_create_conflict_refetches_and_attaches_application() -> None:
    """Verify the create conflict refetches and attaches application scenario."""

    requests: list[httpx.Request] = []
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Simulate the handler operation used by this scenario."""

        nonlocal get_count
        requests.append(request)
        if request.method == "GET":
            get_count += 1
            if get_count == 1:
                return httpx.Response(404)
            return httpx.Response(200, json={"metadata": {"name": "attached"}})
        if request.method == "POST" and request.url.path.endswith("/applications"):
            return httpx.Response(409)
        return httpx.Response(200, json={})

    config = ArgoCdConfig.from_settings(configured_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        client.ensure_application(uuid4(), "attached", ())

    assert [request.method for request in requests] == ["GET", "POST", "GET", "PUT", "POST"]


def test_application_status_is_read_without_triggering_sync() -> None:
    """Verify the application status is read without triggering sync scenario."""

    requests: list[httpx.Request] = []
    response_payload = {
        "metadata": {"name": "attached"},
        "status": {
            "sync": {"status": "Synced", "revision": "abc123"},
            "health": {"status": "Healthy"},
            "operationState": {"phase": "Succeeded", "message": "not exposed"},
            "reconciledAt": "2026-07-19T10:20:30Z",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """Simulate the handler operation used by this scenario."""

        requests.append(request)
        return httpx.Response(200, json=response_payload)

    config = ArgoCdConfig.from_settings(configured_settings())
    instance_id = uuid4()
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        remote = client.get_application_status(instance_id, "attached")

    assert remote.application_name == "attached"
    assert remote.sync_status == "Synced"
    assert remote.health_status == "Healthy"
    assert remote.operation_phase == "Succeeded"
    assert remote.revision == "abc123"
    assert remote.reconciled_at == "2026-07-19T10:20:30Z"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.params["project"] == "coder"


def test_application_status_handles_missing_or_partial_remote_state() -> None:
    """Verify the application status handles missing or partial remote state scenario."""

    responses = iter(
        (
            httpx.Response(200, json={"status": {"sync": {"status": 12}}}),
            httpx.Response(404),
        )
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        """Simulate the handler operation used by this scenario."""

        return next(responses)

    config = ArgoCdConfig.from_settings(configured_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        partial = client.get_application_status(uuid4(), None)
        with pytest.raises(ArgoCdApplicationNotFoundError):
            client.get_application_status(uuid4(), "missing")

    assert partial.sync_status is None
    assert partial.health_status is None
    assert partial.operation_phase is None
    assert partial.revision is None
    assert partial.reconciled_at is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"spec": {}}),
    ],
)
def test_invalid_existing_application_response_is_rejected(response: httpx.Response) -> None:
    """Verify the invalid existing application response is rejected scenario."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Simulate the handler operation used by this scenario."""

        return response

    config = ArgoCdConfig.from_settings(configured_settings())
    with (
        ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ArgoCdRequestError),
    ):
        client.ensure_application(uuid4(), "attached", ())


def test_request_errors_do_not_include_token_or_response_body() -> None:
    """Verify the request errors do not include token or response body scenario."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Simulate the handler operation used by this scenario."""

        return httpx.Response(500, text="super-secret-token private response")

    config = ArgoCdConfig.from_settings(configured_settings())
    with (
        ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ArgoCdRequestError) as caught,
    ):
        client.ensure_application(uuid4(), None, ())

    message = str(caught.value)
    assert "HTTP 500" in message
    assert "super-secret-token" not in message
    assert "private response" not in message
    assert "super-secret-token" not in repr(config)


@pytest.mark.parametrize("skip_ssl_verify", [False, True])
def test_client_tls_and_timeout_configuration(
    monkeypatch: pytest.MonkeyPatch,
    skip_ssl_verify: bool,  # noqa: FBT001
) -> None:
    """Verify the client tls and timeout configuration scenario."""

    captured: dict[str, object] = {}

    class StubClient:
        """Provide the stub client test double for this scenario."""

        def __init__(self, **kwargs: object) -> None:
            """Initialize the test double used by this scenario."""

            captured.update(kwargs)

        def close(self) -> None:
            """Provide the close helper used by this test scenario."""

            captured["closed"] = True

    monkeypatch.setattr(argocd_client.httpx, "Client", StubClient)
    config = ArgoCdConfig.from_settings(configured_settings(argocd_skip_ssl_verify=skip_ssl_verify))
    client = ArgoCdClient(config)
    client.close()

    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 5.0
    assert timeout.read == 30.0
    assert captured["verify"] is not skip_ssl_verify
    assert captured["follow_redirects"] is False
    assert captured["closed"] is True


@pytest.mark.parametrize(
    ("settings", "expected_message"),
    [
        (Settings(), "Missing required Argo CD settings"),
        (
            configured_settings(argocd_application_prefix="x" * 31),
            "APPLICATION_PREFIX",
        ),
        (
            configured_settings(default_admins="alice,,bob"),
            "contains an empty username",
        ),
        (
            configured_settings(default_admins="x" * 256),
            "longer than 255",
        ),
    ],
)
def test_invalid_configuration_is_rejected(
    settings: Settings,
    expected_message: str,
) -> None:
    """Verify the invalid configuration is rejected scenario."""

    with pytest.raises(ArgoCdConfigurationError, match=expected_message):
        ArgoCdConfig.from_settings(settings)
