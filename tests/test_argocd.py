"""Argo CD configuration and HTTP contract tests."""

import json
from typing import Self
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from coder_manager.config import Settings
from coder_manager.domains.argocd import (
    ArgoCdApplicationNotFoundError,
    ArgoCdClient,
    ArgoCdConfig,
    ArgoCdConfigurationError,
    ArgoCdRequestError,
    InstanceHelmValues,
)
from coder_manager.domains.argocd import client as argocd_client
from coder_manager.domains.argocd import service as argocd_service
from coder_manager.domains.argocd.applications import application_name, application_payload

TEST_INSTANCE_SLUG = "k7m4p2x9q3ab"
TEST_APPLICATION_NAME = f"managed-{TEST_INSTANCE_SLUG}"
EXPECTED_INSTANCE_HELM_ARGS = (
    f"--set global.baseDomain={TEST_INSTANCE_SLUG}.code-studio.dev.echonet\n"
    "--set server.config.database.username=db-user\n"
    "--set server.config.database.password=managed\\, secret\n"
    "--set server.config.database.host=postgres.internal\n"
    "--set server.config.database.database=coder\n"
    "--set server.config.database.schema=coder_instance\n"
)


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
        "argocd_development_destination_name": "development-cluster",
        "argocd_staging_destination_name": "staging-cluster",
        "argocd_production_destination_name": "production-cluster",
        "default_admins": " Root.Admin,alice ",
    }
    for environment in ("development", "staging", "production"):
        prefix = f"cyberark_{environment}"
        values.update(
            {
                f"{prefix}_app_id": f"{environment}-app",
                f"{prefix}_cert_name": f"{environment}-cert",
                f"{prefix}_key_name": f"{environment}-key",
                f"{prefix}_safe": f"{environment}-safe",
            }
        )
    values.update(overrides)
    return Settings.model_validate(values)


def instance_helm_values(**overrides: object) -> InstanceHelmValues:
    """Build complete instance-specific Helm values with optional overrides."""

    values: dict[str, object] = {
        "environment": "development",
        "public_url": f"https://{TEST_INSTANCE_SLUG}.code-studio.dev.echonet",
        "database_username": "db-user",
        "database_password": SecretStr("managed, secret"),
        "database_host": "postgres.internal",
        "database_name": "coder",
        "database_schema": "coder_instance",
    }
    values.update(overrides)
    return InstanceHelmValues(**values)  # type: ignore[arg-type]


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
            TEST_INSTANCE_SLUG,
            None,
            (("zoe", "user"), ("alice", "admin")),
            instance_helm_values(),
        )

    assert name == TEST_APPLICATION_NAME
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
        "plugin": {
            "name": "argocd-cyberark-plugin-helm",
            "env": [
                {
                    "name": "HELM_ARGS",
                    "value": (
                        "--namespace app-coder-system\n"
                        "--set policy.config.allowedUsernames="
                        "admin\\,alice\\,root.admin\\,zoe\n"
                        "--set policy.config.adminUsernames=admin\\,alice\\,root.admin\n"
                        f"{EXPECTED_INSTANCE_HELM_ARGS}"
                    ),
                }
            ],
            "parameters": [
                {
                    "name": "cyberark",
                    "map": {
                        "appId": "development-app",
                        "certName": "development-cert",
                        "keyName": "development-key",
                        "safe": "development-safe",
                    },
                }
            ],
        },
    }
    assert "'" not in payload["spec"]["source"]["plugin"]["env"][0]["value"]
    assert payload["spec"]["destination"] == {
        "name": "development-cluster",
        "namespace": "app-coder-system",
    }
    assert payload["spec"]["syncPolicy"] == {
        "automated": {
            "prune": True,
            "selfHeal": True,
        }
    }


def test_policy_username_lists_escape_helm_commas() -> None:
    """Keep comma-separated policy values in one Helm scalar assignment."""

    config = ArgoCdConfig.from_settings(configured_settings(default_admins=""))
    payload = application_payload(
        config,
        TEST_APPLICATION_NAME,
        uuid4(),
        (("h45221", "user"),),
        instance_helm_values(),
    )

    helm_arguments = payload["spec"]["source"]["plugin"]["env"][0]["value"]
    assert "--set policy.config.allowedUsernames=admin\\,h45221\n" in helm_arguments
    assert "--set policy.config.adminUsernames=admin\n" in helm_arguments
    assert "'" not in helm_arguments


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
        returned_name = client.ensure_application(
            uuid4(),
            TEST_INSTANCE_SLUG,
            attached_name,
            (),
            instance_helm_values(environment="staging"),
        )

    assert returned_name == attached_name
    assert [request.method for request in requests] == ["GET", "PUT", "POST"]
    update = json.loads(requests[1].content)
    assert update["metadata"]["resourceVersion"] == "42"
    assert update["metadata"]["annotations"] == {"owner": "platform"}
    assert update["metadata"]["labels"]["existing"] == "kept"
    assert update["metadata"]["labels"]["coder-manager/managed"] == "true"
    assert update["spec"]["project"] == "coder"
    assert update["spec"]["source"]["plugin"]["env"] == [
        {
            "name": "HELM_ARGS",
            "value": (
                "--namespace app-coder-system\n"
                "--set policy.config.allowedUsernames=admin\n"
                "--set policy.config.adminUsernames=admin\n"
                f"{EXPECTED_INSTANCE_HELM_ARGS}"
            ),
        }
    ]
    assert update["spec"]["source"]["plugin"]["parameters"][0]["map"] == {
        "appId": "staging-app",
        "certName": "staging-cert",
        "keyName": "staging-key",
        "safe": "staging-safe",
    }
    assert update["spec"]["destination"] == {
        "name": "staging-cluster",
        "namespace": "app-coder-system",
    }
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
        client.ensure_application(
            uuid4(),
            TEST_INSTANCE_SLUG,
            "attached",
            (),
            instance_helm_values(environment="production"),
        )

    assert [request.method for request in requests] == ["GET", "POST", "GET", "PUT", "POST"]
    update = json.loads(requests[3].content)
    assert update["spec"]["source"]["plugin"]["env"] == [
        {
            "name": "HELM_ARGS",
            "value": (
                "--namespace app-coder-system\n"
                "--set policy.config.allowedUsernames=admin\\,alice\\,root.admin\n"
                "--set policy.config.adminUsernames=admin\\,alice\\,root.admin\n"
                f"{EXPECTED_INSTANCE_HELM_ARGS}"
            ),
        }
    ]
    assert update["spec"]["destination"] == {
        "name": "production-cluster",
        "namespace": "app-coder-system",
    }


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
        remote = client.get_application_status(instance_id, TEST_INSTANCE_SLUG, "attached")

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
        partial = client.get_application_status(uuid4(), TEST_INSTANCE_SLUG, None)
        with pytest.raises(ArgoCdApplicationNotFoundError):
            client.get_application_status(uuid4(), TEST_INSTANCE_SLUG, "missing")

    assert partial.application_name == TEST_APPLICATION_NAME
    assert partial.sync_status is None
    assert partial.health_status is None
    assert partial.operation_phase is None
    assert partial.revision is None
    assert partial.reconciled_at is None


def test_delete_application_is_cascading_and_idempotent() -> None:
    """Delete managed resources and tolerate an already absent Application."""

    requests: list[httpx.Request] = []
    responses = iter((httpx.Response(200, json={}), httpx.Response(404)))

    def handler(request: httpx.Request) -> httpx.Response:
        """Record both the initial deletion and its idempotent retry."""

        requests.append(request)
        return next(responses)

    config = ArgoCdConfig.from_settings(configured_settings())
    instance_id = uuid4()
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        client.delete_application(instance_id, TEST_INSTANCE_SLUG, "attached")
        client.delete_application(instance_id, TEST_INSTANCE_SLUG, "attached")

    assert [(request.method, request.url.path) for request in requests] == [
        ("DELETE", "/root/api/v1/applications/attached"),
        ("DELETE", "/root/api/v1/applications/attached"),
    ]
    assert [dict(request.url.params) for request in requests] == [
        {
            "cascade": "true",
            "propagationPolicy": "foreground",
            "project": "coder",
        },
        {
            "cascade": "true",
            "propagationPolicy": "foreground",
            "project": "coder",
        },
    ]
    assert [request.headers["content-type"] for request in requests] == [
        "application/json",
        "application/json",
    ]
    assert [request.content for request in requests] == [b"", b""]


def test_delete_instance_application_uses_process_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the configured client when the worker invokes the deletion service."""

    deleted: list[tuple[UUID, str | None, str | None]] = []
    instance_id = uuid4()

    class StubClient:
        """Capture calls made by the process-wide deletion service."""

        def __init__(self, _config: ArgoCdConfig) -> None:
            """Accept the validated process configuration."""

        def __enter__(self) -> Self:
            """Enter the client context."""

            return self

        def __exit__(self, *_args: object) -> None:
            """Exit the client context."""

        def delete_application(
            self,
            deleted_id: UUID,
            slug: str | None,
            attached_name: str | None,
        ) -> None:
            """Capture the requested Application deletion."""

            deleted.append((deleted_id, slug, attached_name))

    monkeypatch.setattr(argocd_service, "get_settings", configured_settings)
    monkeypatch.setattr(argocd_service, "ArgoCdClient", StubClient)

    argocd_service.delete_instance_application(instance_id, TEST_INSTANCE_SLUG, "attached")

    assert deleted == [(instance_id, TEST_INSTANCE_SLUG, "attached")]


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
        client.ensure_application(
            uuid4(),
            TEST_INSTANCE_SLUG,
            "attached",
            (),
            instance_helm_values(),
        )


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
        client.ensure_application(
            uuid4(),
            TEST_INSTANCE_SLUG,
            None,
            (),
            instance_helm_values(),
        )

    message = str(caught.value)
    assert "HTTP 500" in message
    assert "super-secret-token" not in message
    assert "private response" not in message
    assert "super-secret-token" not in repr(config)


def test_application_name_prefers_attachment_then_slug_then_legacy_fallback() -> None:
    """Resolve current and historical Application names without renaming attachments."""

    config = ArgoCdConfig.from_settings(configured_settings())
    instance_id = UUID("12345678-1234-5678-1234-567812345678")

    assert application_name(config, instance_id, TEST_INSTANCE_SLUG, "attached") == "attached"
    assert application_name(config, instance_id, TEST_INSTANCE_SLUG, None) == TEST_APPLICATION_NAME
    assert (
        application_name(config, instance_id, None, None)
        == "managed-12345678123456781234567812345678"
    )


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
        (
            configured_settings(argocd_production_destination_name=" "),
            "CODER_MANAGER_ARGOCD_PRODUCTION_DESTINATION_NAME",
        ),
        (
            configured_settings(cyberark_production_safe=" "),
            "CODER_MANAGER_CYBERARK_PRODUCTION_SAFE",
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
