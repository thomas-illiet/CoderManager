"""Argo CD configuration and HTTP contract tests."""

import json
from typing import Self
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretBytes, SecretStr

from coder_manager.config import Settings
from coder_manager.domains.argocd import (
    ArgoCdApplicationNotFoundError,
    ArgoCdClient,
    ArgoCdClientConfig,
    ArgoCdConfig,
    ArgoCdConfigurationError,
    ArgoCdMutationStatus,
    ArgoCdRequestError,
    InstanceHelmValues,
)
from coder_manager.domains.argocd import client as argocd_client
from coder_manager.domains.argocd import service as argocd_service
from coder_manager.domains.argocd.applications import application_name, application_payload

TEST_INSTANCE_SLUG = "k7m4p2x9q3ab"
TEST_APPLICATION_NAME = f"managed-{TEST_INSTANCE_SLUG}"
TEST_ARGOCD_TOKENS = {
    "development": "super-secret-token",
    "staging": "staging-secret-token",
    "production": "production-secret-token",
}
TEST_APPLICATION_PREFIXES = {
    "development": "managed",
    "staging": "managed-staging",
    "production": "managed-production",
}
EXPECTED_INSTANCE_HELM_ARGS = (
    f"--set global.baseDomain={TEST_INSTANCE_SLUG}.code-studio.dev.echonet\n"
    f"--set global.identifier={TEST_INSTANCE_SLUG}\n"
    "--set server.config.postgres.username=<secret:managed-database#username>\n"
    "--set server.config.postgres.password=<secret:managed-database#password>\n"
    "--set server.config.postgres.host=postgres.internal\n"
    "--set server.config.postgres.database=coder\n"
    "--set server.config.postgres.schema=coder_instance\n"
)


def configured_settings(**overrides: object) -> Settings:
    """Build complete Argo CD settings with optional test overrides."""

    values: dict[str, object] = {
        "argocd_url": "https://argocd.test/root/",
        "argocd_region": " emea ",
        "argocd_repository_url": "https://git.test/platform.git",
        "argocd_repository_path": "charts/coder",
        "argocd_target_revision": "v1.2.3",
        "argocd_development_project_name": "development-project",
        "argocd_staging_project_name": "staging-project",
        "argocd_production_project_name": "production-project",
        "argocd_development_destination_name": "development-cluster",
        "argocd_staging_destination_name": "staging-cluster",
        "argocd_production_destination_name": "production-cluster",
        "default_admins": " Root.Admin,alice ",
    }
    for environment in ("development", "staging", "production"):
        prefix = f"cyberark_{environment}"
        values.update(
            {
                f"argocd_{environment}_token": TEST_ARGOCD_TOKENS[environment],
                f"argocd_{environment}_application_prefix": (
                    TEST_APPLICATION_PREFIXES[environment]
                ),
                f"{prefix}_app_id": f"{environment}-app",
                f"{prefix}_cert_name": f"{environment}-cert",
                f"{prefix}_key_name": f"{environment}-key",
                f"{prefix}_safe": f"{environment}-safe",
            }
        )
    values.update(overrides)
    return Settings.model_validate(values)


def client_settings(**overrides: object) -> Settings:
    """Build only the settings required for Argo CD read operations."""

    values: dict[str, object] = {
        "argocd_url": "https://argocd.test/root/",
        "argocd_development_project_name": "development-project",
        "argocd_staging_project_name": "staging-project",
        "argocd_production_project_name": "production-project",
    }
    for environment in ("development", "staging", "production"):
        values.update(
            {
                f"argocd_{environment}_token": TEST_ARGOCD_TOKENS[environment],
                f"argocd_{environment}_application_prefix": (
                    TEST_APPLICATION_PREFIXES[environment]
                ),
            }
        )
    values.update(overrides)
    return Settings.model_validate(values)


def instance_helm_values(**overrides: object) -> InstanceHelmValues:
    """Build complete instance-specific Helm values with optional overrides."""

    values: dict[str, object] = {
        "slug": TEST_INSTANCE_SLUG,
        "environment": "development",
        "public_url": f"https://{TEST_INSTANCE_SLUG}.code-studio.dev.echonet",
        "database_username": "db-user",
        "database_password": SecretStr("managed, secret"),
        "database_host": "postgres.internal",
        "database_name": "coder",
        "managed_database_name": "managed-database",
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
        result = client.ensure_application(
            instance_id,
            TEST_INSTANCE_SLUG,
            None,
            (("zoe", "user"), ("alice", "admin")),
            instance_helm_values(),
        )

    assert result.status is ArgoCdMutationStatus.COMPLETED
    assert result.application_name == TEST_APPLICATION_NAME
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", f"/root/api/v1/applications/{result.application_name}"),
        ("POST", "/root/api/v1/applications"),
        ("GET", f"/root/api/v1/applications/{result.application_name}"),
        ("POST", f"/root/api/v1/applications/{result.application_name}/sync"),
    ]
    assert all(
        request.headers["authorization"] == "Bearer super-secret-token" for request in requests
    )
    assert [dict(request.url.params) for request in requests] == [
        {"project": "development-project"},
        {"upsert": "false", "validate": "true"},
        {"project": "development-project"},
        {"project": "development-project"},
    ]
    payload = json.loads(requests[1].content)
    assert payload["spec"]["project"] == "development-project"
    assert payload["metadata"] == {
        "name": result.application_name,
        "labels": {
            "coder-manager/instance-id": str(instance_id),
            "environment": "development",
            "region": "EMEA",
            "domain": "code-station",
            "tier": "standard",
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
                        "--values values-dev.yaml\n"
                        "--namespace app-code-instance\n"
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
                        "region": "EMEA",
                        "safe": "development-safe",
                    },
                }
            ],
        },
    }
    helm_arguments = payload["spec"]["source"]["plugin"]["env"][0]["value"]
    assert "'" not in helm_arguments
    assert "db-user" not in helm_arguments
    assert "managed\\, secret" not in helm_arguments
    assert "<secret:managed-database#username>" in helm_arguments
    assert "<secret:managed-database#password>" in helm_arguments
    assert payload["spec"]["destination"] == {
        "name": "development-cluster",
        "namespace": "app-code-instance",
    }
    assert payload["spec"]["syncPolicy"] == {
        "automated": {
            "prune": True,
            "selfHeal": True,
        }
    }


@pytest.mark.parametrize(
    ("environment", "values_file", "project"),
    [
        ("development", "values-dev.yaml", "development-project"),
        ("staging", "values-stg.yaml", "staging-project"),
        ("production", "values-prd.yaml", "production-project"),
    ],
)
def test_helm_values_file_and_project_match_environment(
    environment: str,
    values_file: str,
    project: str,
) -> None:
    """Select exactly one environment-specific Helm values file."""

    config = ArgoCdConfig.from_settings(configured_settings(default_admins=""))
    payload = application_payload(
        config,
        TEST_APPLICATION_NAME,
        uuid4(),
        (),
        instance_helm_values(environment=environment),
    )

    helm_arguments = payload["spec"]["source"]["plugin"]["env"][0]["value"]
    assert helm_arguments.startswith(f"--values {values_file}\n")
    assert helm_arguments.count("--values ") == 1
    assert payload["spec"]["project"] == project
    assert payload["metadata"]["labels"]["environment"] == environment


def test_database_secret_references_use_managed_database_name() -> None:
    """Resolve database credentials from the allocated managed database secret."""

    config = ArgoCdConfig.from_settings(configured_settings(default_admins=""))
    payload = application_payload(
        config,
        TEST_APPLICATION_NAME,
        uuid4(),
        (),
        instance_helm_values(
            database_name="actual-postgres-database",
            managed_database_name="managed-database-42",
        ),
    )

    helm_arguments = payload["spec"]["source"]["plugin"]["env"][0]["value"]
    assert (
        "--set server.config.postgres.username=<secret:managed-database-42#username>\n"
        in helm_arguments
    )
    assert (
        "--set server.config.postgres.password=<secret:managed-database-42#password>\n"
        in helm_arguments
    )
    assert "--set server.config.postgres.database=actual-postgres-database\n" in helm_arguments


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


@pytest.mark.parametrize(
    ("kubeconfig", "encoded"),
    [
        (b"\x00\xffarbitrary\nkubeconfig", "AP9hcmJpdHJhcnkKa3ViZWNvbmZpZw=="),
        (b"", ""),
    ],
)
def test_kubeconfig_is_appended_to_helm_arguments_as_base64(
    kubeconfig: bytes,
    encoded: str,
) -> None:
    """Append uploaded bytes as one exact Base64 Helm scalar, including an empty file."""

    config = ArgoCdConfig.from_settings(configured_settings(default_admins=""))
    payload = application_payload(
        config,
        TEST_APPLICATION_NAME,
        uuid4(),
        (),
        instance_helm_values(kubeconfig=SecretBytes(kubeconfig)),
    )

    helm_arguments = payload["spec"]["source"]["plugin"]["env"][0]["value"]
    assert helm_arguments.endswith(f"--set server.config.kube={encoded}\n")
    assert helm_arguments.count("--set server.config.kube=") == 1


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
            "labels": {
                "existing": "kept",
                "coder-manager/managed": "true",
            },
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
    instance_id = uuid4()
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        result = client.ensure_application(
            instance_id,
            TEST_INSTANCE_SLUG,
            attached_name,
            (),
            instance_helm_values(environment="staging"),
        )

    assert result.status is ArgoCdMutationStatus.COMPLETED
    assert result.application_name == attached_name
    assert [request.method for request in requests] == ["GET", "PUT", "GET", "POST"]
    update = json.loads(requests[1].content)
    assert update["metadata"]["resourceVersion"] == "42"
    assert update["metadata"]["annotations"] == {"owner": "platform"}
    assert update["metadata"]["labels"] == {
        "existing": "kept",
        "coder-manager/instance-id": str(instance_id),
        "environment": "staging",
        "region": "EMEA",
        "domain": "code-station",
        "tier": "standard",
    }
    assert update["spec"]["project"] == "staging-project"
    assert [dict(request.url.params) for request in requests] == [
        {"project": "staging-project"},
        {"project": "staging-project", "validate": "true"},
        {"project": "staging-project"},
        {"project": "staging-project"},
    ]
    assert all(
        request.headers["authorization"] == "Bearer staging-secret-token" for request in requests
    )
    assert update["spec"]["source"]["plugin"]["env"] == [
        {
            "name": "HELM_ARGS",
            "value": (
                "--values values-stg.yaml\n"
                "--namespace app-code-instance\n"
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
        "region": "EMEA",
        "safe": "staging-safe",
    }
    assert update["spec"]["destination"] == {
        "name": "staging-cluster",
        "namespace": "app-code-instance",
    }
    assert "status" not in update


@pytest.mark.parametrize("phase", ["Running", "Terminating"])
def test_active_application_operation_defers_reconciliation(phase: str) -> None:
    """Avoid every reconciliation mutation while Argo CD is already processing."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return one existing Application with an active operation."""

        requests.append(request)
        return httpx.Response(
            200,
            json={
                "metadata": {"name": TEST_APPLICATION_NAME},
                "status": {"operationState": {"phase": phase}},
            },
        )

    config = ArgoCdConfig.from_settings(configured_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        result = client.ensure_application(
            uuid4(),
            TEST_INSTANCE_SLUG,
            None,
            (),
            instance_helm_values(),
        )

    assert result.status is ArgoCdMutationStatus.DEFERRED
    assert result.application_name == TEST_APPLICATION_NAME
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", f"/root/api/v1/applications/{TEST_APPLICATION_NAME}")
    ]


def test_operation_started_by_update_defers_explicit_sync() -> None:
    """Re-check after PUT and avoid a redundant sync when automation starts first."""

    requests: list[httpx.Request] = []
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Become active only after the Application update."""

        nonlocal get_count
        requests.append(request)
        if request.method == "GET":
            get_count += 1
            phase = "Succeeded" if get_count == 1 else "Running"
            return httpx.Response(
                200,
                json={
                    "metadata": {"name": TEST_APPLICATION_NAME},
                    "status": {"operationState": {"phase": phase}},
                },
            )
        return httpx.Response(200, json={})

    config = ArgoCdConfig.from_settings(configured_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        result = client.ensure_application(
            uuid4(),
            TEST_INSTANCE_SLUG,
            None,
            (),
            instance_helm_values(),
        )

    assert result.status is ArgoCdMutationStatus.DEFERRED
    assert [request.method for request in requests] == ["GET", "PUT", "GET"]


@pytest.mark.parametrize("phase", ["Succeeded", "Failed", "Error", "Unknown"])
def test_terminal_or_unknown_operation_phase_allows_reconciliation(phase: str) -> None:
    """Keep normal reconciliation behavior for every non-active operation phase."""

    requests: list[httpx.Request] = []
    existing = {
        "metadata": {"name": TEST_APPLICATION_NAME},
        "status": {"operationState": {"phase": phase}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """Return one non-active existing Application."""

        requests.append(request)
        return httpx.Response(200, json=existing)

    config = ArgoCdConfig.from_settings(configured_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        result = client.ensure_application(
            uuid4(),
            TEST_INSTANCE_SLUG,
            None,
            (),
            instance_helm_values(),
        )

    assert result.status is ArgoCdMutationStatus.COMPLETED
    assert [request.method for request in requests] == ["GET", "PUT", "GET", "POST"]


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

    assert [request.method for request in requests] == [
        "GET",
        "POST",
        "GET",
        "PUT",
        "GET",
        "POST",
    ]
    update = json.loads(requests[3].content)
    assert update["spec"]["source"]["plugin"]["env"] == [
        {
            "name": "HELM_ARGS",
            "value": (
                "--values values-prd.yaml\n"
                "--namespace app-code-instance\n"
                "--set policy.config.allowedUsernames=admin\\,alice\\,root.admin\n"
                "--set policy.config.adminUsernames=admin\\,alice\\,root.admin\n"
                f"{EXPECTED_INSTANCE_HELM_ARGS}"
            ),
        }
    ]
    assert update["spec"]["destination"] == {
        "name": "production-cluster",
        "namespace": "app-code-instance",
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

    config = ArgoCdClientConfig.from_settings(client_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        remote = client.get_application_status(TEST_INSTANCE_SLUG, "attached", "production")

    assert remote.application_name == "attached"
    assert remote.sync_status == "Synced"
    assert remote.health_status == "Healthy"
    assert remote.operation_phase == "Succeeded"
    assert remote.revision == "abc123"
    assert remote.reconciled_at == "2026-07-19T10:20:30Z"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.params["project"] == "production-project"


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

    config = ArgoCdClientConfig.from_settings(client_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        partial = client.get_application_status(TEST_INSTANCE_SLUG, None, "development")
        with pytest.raises(ArgoCdApplicationNotFoundError):
            client.get_application_status(TEST_INSTANCE_SLUG, "missing", "development")

    assert partial.application_name == TEST_APPLICATION_NAME
    assert partial.sync_status is None
    assert partial.health_status is None
    assert partial.operation_phase is None
    assert partial.revision is None
    assert partial.reconciled_at is None


def test_delete_application_is_cascading_and_idempotent() -> None:
    """Delete managed resources and tolerate an already absent Application."""

    requests: list[httpx.Request] = []
    responses = iter(
        (
            httpx.Response(200, json={"metadata": {"name": "attached"}}),
            httpx.Response(200, json={}),
            httpx.Response(404),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        """Record both the initial deletion and its idempotent retry."""

        requests.append(request)
        return next(responses)

    config = ArgoCdConfig.from_settings(configured_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        first = client.delete_application(TEST_INSTANCE_SLUG, "attached", "production")
        second = client.delete_application(TEST_INSTANCE_SLUG, "attached", "production")

    assert first is ArgoCdMutationStatus.COMPLETED
    assert second is ArgoCdMutationStatus.COMPLETED
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/root/api/v1/applications/attached"),
        ("DELETE", "/root/api/v1/applications/attached"),
        ("GET", "/root/api/v1/applications/attached"),
    ]
    assert [dict(request.url.params) for request in requests] == [
        {"project": "production-project"},
        {
            "cascade": "true",
            "propagationPolicy": "foreground",
            "project": "production-project",
        },
        {"project": "production-project"},
    ]
    assert requests[1].headers["content-type"] == "application/json"
    assert all(
        request.headers["authorization"] == "Bearer production-secret-token" for request in requests
    )
    assert requests[1].content == b""


def test_read_status_service_uses_only_client_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read status without requiring worker deployment or CyberArk settings."""

    captured: list[ArgoCdClientConfig] = []

    class StubClient:
        """Capture the configuration used by the API status service."""

        def __init__(self, config: ArgoCdClientConfig) -> None:
            """Store the minimal validated client configuration."""

            captured.append(config)

        def __enter__(self) -> Self:
            """Enter the client context."""

            return self

        def __exit__(self, *_args: object) -> None:
            """Exit the client context."""

        def get_application_status(
            self,
            _slug: str,
            _attached_name: str | None,
            _environment: str,
        ) -> str:
            """Return a sentinel status value."""

            return "status"

    monkeypatch.setattr(argocd_service, "ArgoCdClient", StubClient)

    result = argocd_service.read_instance_application_status(
        TEST_INSTANCE_SLUG,
        None,
        "staging",
        client_settings(),
    )

    assert result == "status"
    assert len(captured) == 1
    assert type(captured[0]) is ArgoCdClientConfig


@pytest.mark.parametrize("phase", ["Running", "Terminating"])
def test_active_application_operation_defers_deletion(phase: str) -> None:
    """Do not issue DELETE while Argo CD is processing the Application."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return one existing Application with an active operation."""

        requests.append(request)
        return httpx.Response(
            200,
            json={
                "metadata": {"name": "attached"},
                "status": {"operationState": {"phase": phase}},
            },
        )

    config = ArgoCdConfig.from_settings(configured_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        result = client.delete_application(TEST_INSTANCE_SLUG, "attached", "development")

    assert result is ArgoCdMutationStatus.DEFERRED
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/root/api/v1/applications/attached")
    ]


def test_delete_instance_application_uses_process_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the configured client when the worker invokes the deletion service."""

    deleted: list[tuple[str, str | None, str]] = []

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
            slug: str,
            attached_name: str | None,
            environment: str,
        ) -> ArgoCdMutationStatus:
            """Capture the requested Application deletion."""

            deleted.append((slug, attached_name, environment))
            return ArgoCdMutationStatus.COMPLETED

    monkeypatch.setattr(argocd_service, "get_settings", configured_settings)
    monkeypatch.setattr(argocd_service, "ArgoCdClient", StubClient)

    result = argocd_service.delete_instance_application(
        TEST_INSTANCE_SLUG,
        "attached",
        "staging",
    )

    assert deleted == [(TEST_INSTANCE_SLUG, "attached", "staging")]
    assert result is ArgoCdMutationStatus.COMPLETED


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


def test_request_errors_include_exact_argocd_message_only() -> None:
    """Include Argo CD's complete JSON message without exposing sibling response fields."""

    remote_message = "application spec is invalid\n" + "full validation detail " * 250

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a missing Application followed by an Argo CD validation error."""

        if request.method == "GET":
            return httpx.Response(httpx.codes.NOT_FOUND)
        return httpx.Response(
            httpx.codes.BAD_REQUEST,
            json={
                "error": "private sibling error",
                "code": 3,
                "message": remote_message,
                "details": ["private sibling detail"],
            },
        )

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

    assert str(caught.value) == (
        f"Argo CD POST api/v1/applications returned HTTP 400: {remote_message}"
    )
    assert "private sibling error" not in str(caught.value)
    assert "private sibling detail" not in str(caught.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(httpx.codes.BAD_REQUEST, text="private plain-text response"),
        httpx.Response(httpx.codes.BAD_REQUEST, content=b"{"),
        httpx.Response(httpx.codes.BAD_REQUEST, json=[]),
        httpx.Response(httpx.codes.BAD_REQUEST, json={"error": "private error"}),
        httpx.Response(httpx.codes.BAD_REQUEST, json={"message": ""}),
        httpx.Response(httpx.codes.BAD_REQUEST, json={"message": "   "}),
        httpx.Response(httpx.codes.BAD_REQUEST, json={"message": 42}),
    ],
)
def test_request_errors_without_json_message_remain_generic(response: httpx.Response) -> None:
    """Keep the generic error for every response without a usable JSON message."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a missing Application followed by the parameterized invalid response."""

        if request.method == "GET":
            return httpx.Response(httpx.codes.NOT_FOUND)
        return response

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

    assert str(caught.value) == "Argo CD POST api/v1/applications returned HTTP 400"


def test_request_errors_do_not_include_token_or_unstructured_body() -> None:
    """Exclude tokens and bodies that do not provide a structured JSON message."""

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


def test_application_name_prefers_attachment_then_strict_slug() -> None:
    """Resolve attached and environment-specific strict slug Application names."""

    config = ArgoCdClientConfig.from_settings(client_settings())

    assert application_name(config, TEST_INSTANCE_SLUG, "attached", "production") == "attached"
    assert (
        application_name(config, TEST_INSTANCE_SLUG, None, "development") == TEST_APPLICATION_NAME
    )
    assert (
        application_name(config, TEST_INSTANCE_SLUG, None, "staging")
        == f"managed-staging-{TEST_INSTANCE_SLUG}"
    )
    assert (
        application_name(config, TEST_INSTANCE_SLUG, None, "production")
        == f"managed-production-{TEST_INSTANCE_SLUG}"
    )


@pytest.mark.parametrize("environment", ["development", "staging", "production"])
def test_client_configuration_selects_environment_secrets_and_prefixes(
    environment: str,
) -> None:
    """Select immutable environment-specific authentication and naming settings."""

    config = ArgoCdClientConfig.from_settings(client_settings())

    assert config.token_for(environment) == TEST_ARGOCD_TOKENS[environment]
    assert config.application_prefix_for(environment) == TEST_APPLICATION_PREFIXES[environment]
    assert TEST_ARGOCD_TOKENS[environment] not in repr(config)


def test_one_client_switches_authorization_between_environments() -> None:
    """Build authentication per request without leaking a previous environment token."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record environment-specific existence requests."""

        requests.append(request)
        return httpx.Response(404)

    config = ArgoCdClientConfig.from_settings(client_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        for environment in ("development", "staging", "production"):
            assert not client.application_exists(TEST_INSTANCE_SLUG, None, environment)

    assert [request.headers["authorization"] for request in requests] == [
        "Bearer super-secret-token",
        "Bearer staging-secret-token",
        "Bearer production-secret-token",
    ]


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
    config = ArgoCdClientConfig.from_settings(
        client_settings(argocd_skip_ssl_verify=skip_ssl_verify)
    )
    client = ArgoCdClient(config)
    client.close()

    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 5.0
    assert timeout.read == 30.0
    assert captured["verify"] is not skip_ssl_verify
    assert captured["follow_redirects"] is False
    assert captured["closed"] is True


def test_client_configuration_cannot_reconcile_application() -> None:
    """Reject reconciliation when only read-level settings were provided."""

    config = ArgoCdClientConfig.from_settings(client_settings())
    with (
        ArgoCdClient(
            config, transport=httpx.MockTransport(lambda _request: httpx.Response(200))
        ) as client,
        pytest.raises(ArgoCdConfigurationError, match="deployment settings are required"),
    ):
        client.ensure_application(
            uuid4(),
            TEST_INSTANCE_SLUG,
            None,
            (),
            instance_helm_values(),
        )


@pytest.mark.parametrize(
    ("settings", "expected_message"),
    [
        (Settings(), "Missing required Argo CD client settings"),
        (
            client_settings(argocd_development_application_prefix="x" * 31),
            "CODER_MANAGER_ARGOCD_DEVELOPMENT_APPLICATION_PREFIX",
        ),
        (
            client_settings(argocd_staging_project_name=" "),
            "CODER_MANAGER_ARGOCD_STAGING_PROJECT_NAME",
        ),
    ],
)
def test_invalid_client_configuration_is_rejected(
    settings: Settings,
    expected_message: str,
) -> None:
    """Reject incomplete or invalid read-level Argo CD settings."""

    with pytest.raises(ArgoCdConfigurationError, match=expected_message):
        ArgoCdClientConfig.from_settings(settings)


@pytest.mark.parametrize("environment", ["development", "staging", "production"])
def test_environment_token_is_required(environment: str) -> None:
    """Reject an empty bearer token for every supported environment."""

    settings = client_settings(**{f"argocd_{environment}_token": " "})

    with pytest.raises(
        ArgoCdConfigurationError,
        match=f"CODER_MANAGER_ARGOCD_{environment.upper()}_TOKEN",
    ):
        ArgoCdClientConfig.from_settings(settings)


@pytest.mark.parametrize("environment", ["development", "staging", "production"])
@pytest.mark.parametrize("prefix", [" ", "x" * 31])
def test_environment_application_prefix_is_required_and_valid(
    environment: str,
    prefix: str,
) -> None:
    """Reject an empty or invalid Application prefix for every environment."""

    settings = client_settings(**{f"argocd_{environment}_application_prefix": prefix})

    with pytest.raises(
        ArgoCdConfigurationError,
        match=f"CODER_MANAGER_ARGOCD_{environment.upper()}_APPLICATION_PREFIX",
    ):
        ArgoCdClientConfig.from_settings(settings)


def test_legacy_global_token_and_prefix_settings_are_not_supported() -> None:
    """Reject removed global authentication and naming settings without fallback."""

    settings = Settings.model_validate(
        {
            "argocd_url": "https://argocd.test",
            "argocd_token": "legacy-token",
            "argocd_application_prefix": "legacy-prefix",
        }
    )

    assert "argocd_token" not in Settings.model_fields
    assert "argocd_application_prefix" not in Settings.model_fields
    with pytest.raises(
        ArgoCdConfigurationError,
        match="CODER_MANAGER_ARGOCD_DEVELOPMENT_TOKEN",
    ):
        ArgoCdClientConfig.from_settings(settings)


def test_legacy_global_project_setting_is_not_supported() -> None:
    """Require environment projects even when the removed global setting is supplied."""

    settings = client_settings(
        argocd_development_project_name=None,
        argocd_project="legacy-project",
    )

    assert "argocd_project" not in Settings.model_fields
    with pytest.raises(
        ArgoCdConfigurationError,
        match="CODER_MANAGER_ARGOCD_DEVELOPMENT_PROJECT_NAME",
    ):
        ArgoCdClientConfig.from_settings(settings)


@pytest.mark.parametrize(
    ("settings", "expected_message"),
    [
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
            configured_settings(argocd_region=" "),
            "CODER_MANAGER_ARGOCD_REGION",
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
