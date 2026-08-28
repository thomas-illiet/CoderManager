"""Argo CD configuration and HTTP contract tests."""

import json
from typing import Self
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretBytes, SecretStr, ValidationError

from coder_manager.config import Environment, Settings
from coder_manager.domains.argocd import (
    ArgoCdApplicationNotFoundError,
    ArgoCdApplicationOwnershipError,
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
TEST_INSTANCE_ID = UUID("12345678-1234-5678-1234-567812345678")
TEST_APPLICATION_NAME = f"managed-{TEST_INSTANCE_SLUG}"
TEST_ARGOCD_TOKEN = "super-secret-token"  # noqa: S105
TEST_APPLICATION_PREFIX = "managed"
EXPECTED_INSTANCE_HELM_ARGS = (
    f"--set global.baseDomain={TEST_INSTANCE_SLUG}.emea.code-studio.dev.echonet\n"
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
        "environment": "development",
        "instance_base_domain": "emea.code-studio.dev.echonet",
        "argocd_url": "https://argocd.test/root/",
        "argocd_token": TEST_ARGOCD_TOKEN,
        "argocd_project_name": "coder-project",
        "argocd_application_prefix": TEST_APPLICATION_PREFIX,
        "argocd_region": " emea ",
        "argocd_repository_url": "https://git.test/platform.git",
        "argocd_repository_path": "charts/coder",
        "argocd_target_revision": "v1.2.3",
        "argocd_destination_name": "coder-cluster",
        "cyberark_app_id": "coder-app",
        "cyberark_cert_name": "coder-cert",
        "cyberark_key_name": "coder-key",
        "cyberark_safe": "coder-safe",
        "default_admins": " Root.Admin,alice ",
    }
    values.update(overrides)
    return Settings.model_validate(values)


def client_settings(**overrides: object) -> Settings:
    """Build only the settings required for Argo CD read operations."""

    values: dict[str, object] = {
        "environment": "development",
        "instance_base_domain": "emea.code-studio.dev.echonet",
        "argocd_url": "https://argocd.test/root/",
        "argocd_token": TEST_ARGOCD_TOKEN,
        "argocd_project_name": "coder-project",
        "argocd_application_prefix": TEST_APPLICATION_PREFIX,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def instance_helm_values(**overrides: object) -> InstanceHelmValues:
    """Build complete instance-specific Helm values with optional overrides."""

    values: dict[str, object] = {
        "slug": TEST_INSTANCE_SLUG,
        "public_url": f"https://{TEST_INSTANCE_SLUG}.emea.code-studio.dev.echonet",
        "database_username": "db-user",
        "database_password": SecretStr("managed, secret"),
        "database_host": "postgres.internal",
        "database_name": "coder",
        "managed_database_name": "managed-database",
        "database_schema": "coder_instance",
    }
    values.update(overrides)
    return InstanceHelmValues(**values)  # type: ignore[arg-type]


def owned_labels(instance_id: UUID = TEST_INSTANCE_ID) -> dict[str, str]:
    """Return the strict ownership labels for this deployment."""

    return {
        "coder-manager/instance-id": str(instance_id),
        "environment": "development",
    }


@pytest.mark.parametrize(
    ("raw_environment", "expected"),
    [
        ("development", Environment.DEVELOPMENT),
        ("staging", Environment.STAGING),
        ("production", Environment.PRODUCTION),
    ],
)
def test_environment_setting_is_a_strict_infrastructure_enum(
    raw_environment: str,
    expected: Environment,
) -> None:
    """Accept only the three deployment environments exposed by configuration."""

    assert Settings(environment=raw_environment).environment is expected  # type: ignore[arg-type]


def test_environment_setting_rejects_unknown_values() -> None:
    """Reject an infrastructure environment outside the public enum."""

    with pytest.raises(ValidationError, match="development"):
        Settings(environment="testing")  # type: ignore[arg-type]


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
    instance_id = TEST_INSTANCE_ID
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
        {"project": "coder-project"},
        {"upsert": "false", "validate": "true"},
        {"project": "coder-project"},
        {"project": "coder-project"},
    ]
    payload = json.loads(requests[1].content)
    assert payload["spec"]["project"] == "coder-project"
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
                        "appId": "coder-app",
                        "certName": "coder-cert",
                        "keyName": "coder-key",
                        "region": "EMEA",
                        "safe": "coder-safe",
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
        "name": "coder-cluster",
        "namespace": "app-code-instance",
    }
    assert payload["spec"]["syncPolicy"] == {
        "automated": {
            "prune": True,
            "selfHeal": True,
        }
    }


def test_helm_payload_uses_global_project_and_no_values_file() -> None:
    """Use the deployment project without selecting an environment values file."""

    config = ArgoCdConfig.from_settings(configured_settings(default_admins=""))
    payload = application_payload(
        config,
        TEST_APPLICATION_NAME,
        uuid4(),
        (),
        instance_helm_values(),
    )

    helm_arguments = payload["spec"]["source"]["plugin"]["env"][0]["value"]
    assert "--values " not in helm_arguments
    assert payload["spec"]["project"] == "coder-project"
    assert payload["metadata"]["labels"]["environment"] == "development"


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
    ("field_name", "value"),
    [
        ("database_host", "postgres.internal\n--set global.baseDomain=evil"),
        ("database_name", "coder\r--set global.identifier=evil"),
        ("managed_database_name", "managed\n--set server.config.kube=evil"),
    ],
)
def test_helm_scalar_values_reject_line_breaks(field_name: str, value: str) -> None:
    """Reject values that could append a second Helm command-line argument."""

    config = ArgoCdConfig.from_settings(configured_settings(default_admins=""))

    with pytest.raises(ArgoCdRequestError, match="cannot contain line breaks"):
        application_payload(
            config,
            TEST_APPLICATION_NAME,
            uuid4(),
            (),
            instance_helm_values(**{field_name: value}),
        )


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
                "coder-manager/instance-id": str(TEST_INSTANCE_ID),
                "environment": "development",
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
    instance_id = TEST_INSTANCE_ID
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        result = client.ensure_application(
            instance_id,
            TEST_INSTANCE_SLUG,
            attached_name,
            (),
            instance_helm_values(),
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
        "environment": "development",
        "region": "EMEA",
        "domain": "code-station",
        "tier": "standard",
    }
    assert update["spec"]["project"] == "coder-project"
    assert [dict(request.url.params) for request in requests] == [
        {"project": "coder-project"},
        {"project": "coder-project", "validate": "true"},
        {"project": "coder-project"},
        {"project": "coder-project"},
    ]
    assert all(
        request.headers["authorization"] == "Bearer super-secret-token" for request in requests
    )
    assert update["spec"]["source"]["plugin"]["env"] == [
        {
            "name": "HELM_ARGS",
            "value": (
                "--namespace app-code-instance\n"
                "--set policy.config.allowedUsernames=admin\n"
                "--set policy.config.adminUsernames=admin\n"
                f"{EXPECTED_INSTANCE_HELM_ARGS}"
            ),
        }
    ]
    assert update["spec"]["source"]["plugin"]["parameters"][0]["map"] == {
        "appId": "coder-app",
        "certName": "coder-cert",
        "keyName": "coder-key",
        "region": "EMEA",
        "safe": "coder-safe",
    }
    assert update["spec"]["destination"] == {
        "name": "coder-cluster",
        "namespace": "app-code-instance",
    }
    assert "status" not in update


@pytest.mark.parametrize(
    "labels",
    [
        {},
        {
            "coder-manager/instance-id": str(uuid4()),
            "environment": "development",
        },
        {
            "coder-manager/instance-id": str(TEST_INSTANCE_ID),
            "environment": "staging",
        },
    ],
)
def test_reconciliation_rejects_unowned_application(labels: dict[str, str]) -> None:
    """Never overwrite or defer an Application without exact instance ownership."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return an active but unowned Application."""

        requests.append(request)
        return httpx.Response(
            200,
            json={
                "metadata": {"name": TEST_APPLICATION_NAME, "labels": labels},
                "status": {"operationState": {"phase": "Running"}},
            },
        )

    config = ArgoCdConfig.from_settings(configured_settings())
    with (
        ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ArgoCdApplicationOwnershipError, match="is not owned"),
    ):
        client.ensure_application(
            TEST_INSTANCE_ID,
            TEST_INSTANCE_SLUG,
            None,
            (),
            instance_helm_values(),
        )

    assert [request.method for request in requests] == ["GET"]


def test_create_conflict_rejects_application_owned_by_another_instance() -> None:
    """Fence the adoption race after Argo reports a create conflict."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a foreign owner only after the conflicting create."""

        requests.append(request)
        if request.method == "GET" and len(requests) == 1:
            return httpx.Response(404)
        if request.method == "POST":
            return httpx.Response(409)
        return httpx.Response(
            200,
            json={
                "metadata": {
                    "name": TEST_APPLICATION_NAME,
                    "labels": owned_labels(uuid4()),
                }
            },
        )

    config = ArgoCdConfig.from_settings(configured_settings())
    with (
        ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ArgoCdApplicationOwnershipError, match="is not owned"),
    ):
        client.ensure_application(
            TEST_INSTANCE_ID,
            TEST_INSTANCE_SLUG,
            None,
            (),
            instance_helm_values(),
        )

    assert [request.method for request in requests] == ["GET", "POST", "GET"]


def test_deletion_rejects_unowned_application() -> None:
    """Never delete an Application without exact instance ownership."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return one Application whose ownership label is missing."""

        requests.append(request)
        return httpx.Response(200, json={"metadata": {"name": "attached"}})

    config = ArgoCdConfig.from_settings(configured_settings())
    with (
        ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ArgoCdApplicationOwnershipError, match="is not owned"),
    ):
        client.delete_application(
            TEST_INSTANCE_ID,
            TEST_INSTANCE_SLUG,
            "attached",
        )

    assert [request.method for request in requests] == ["GET"]


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
                "metadata": {
                    "name": TEST_APPLICATION_NAME,
                    "labels": owned_labels(),
                },
                "status": {"operationState": {"phase": phase}},
            },
        )

    config = ArgoCdConfig.from_settings(configured_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        result = client.ensure_application(
            TEST_INSTANCE_ID,
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
                    "metadata": {
                        "name": TEST_APPLICATION_NAME,
                        "labels": owned_labels(),
                    },
                    "status": {"operationState": {"phase": phase}},
                },
            )
        return httpx.Response(200, json={})

    config = ArgoCdConfig.from_settings(configured_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        result = client.ensure_application(
            TEST_INSTANCE_ID,
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
        "metadata": {
            "name": TEST_APPLICATION_NAME,
            "labels": owned_labels(),
        },
        "status": {"operationState": {"phase": phase}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """Return one non-active existing Application."""

        requests.append(request)
        return httpx.Response(200, json=existing)

    config = ArgoCdConfig.from_settings(configured_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        result = client.ensure_application(
            TEST_INSTANCE_ID,
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
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "name": "attached",
                        "labels": owned_labels(),
                    }
                },
            )
        if request.method == "POST" and request.url.path.endswith("/applications"):
            return httpx.Response(409)
        return httpx.Response(200, json={})

    config = ArgoCdConfig.from_settings(configured_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        client.ensure_application(
            TEST_INSTANCE_ID,
            TEST_INSTANCE_SLUG,
            "attached",
            (),
            instance_helm_values(),
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
                "--namespace app-code-instance\n"
                "--set policy.config.allowedUsernames=admin\\,alice\\,root.admin\n"
                "--set policy.config.adminUsernames=admin\\,alice\\,root.admin\n"
                f"{EXPECTED_INSTANCE_HELM_ARGS}"
            ),
        }
    ]
    assert update["spec"]["destination"] == {
        "name": "coder-cluster",
        "namespace": "app-code-instance",
    }


def test_application_status_is_read_without_triggering_sync() -> None:
    """Verify the application status is read without triggering sync scenario."""

    requests: list[httpx.Request] = []
    response_payload = {
        "metadata": {
            "name": "attached",
            "labels": owned_labels(),
        },
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
        remote = client.get_application_status(
            TEST_INSTANCE_ID,
            TEST_INSTANCE_SLUG,
            "attached",
        )

    assert remote.application_name == "attached"
    assert remote.sync_status == "Synced"
    assert remote.health_status == "Healthy"
    assert remote.operation_phase == "Succeeded"
    assert remote.revision == "abc123"
    assert remote.reconciled_at == "2026-07-19T10:20:30Z"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.params["project"] == "coder-project"


def test_application_status_handles_missing_or_partial_remote_state() -> None:
    """Verify the application status handles missing or partial remote state scenario."""

    responses = iter(
        (
            httpx.Response(
                200,
                json={
                    "metadata": {"labels": owned_labels()},
                    "status": {"sync": {"status": 12}},
                },
            ),
            httpx.Response(404),
        )
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        """Simulate the handler operation used by this scenario."""

        return next(responses)

    config = ArgoCdClientConfig.from_settings(client_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        partial = client.get_application_status(
            TEST_INSTANCE_ID,
            TEST_INSTANCE_SLUG,
            None,
        )
        with pytest.raises(ArgoCdApplicationNotFoundError):
            client.get_application_status(
                TEST_INSTANCE_ID,
                TEST_INSTANCE_SLUG,
                "missing",
            )

    assert partial.application_name == TEST_APPLICATION_NAME
    assert partial.sync_status is None
    assert partial.health_status is None
    assert partial.operation_phase is None
    assert partial.revision is None
    assert partial.reconciled_at is None


def test_delete_application_is_cascading_and_idempotent() -> None:
    """Wait for confirmed absence and tolerate an already deleted Application."""

    requests: list[httpx.Request] = []
    responses = iter(
        (
            httpx.Response(
                200,
                json={
                    "metadata": {
                        "name": "attached",
                        "labels": owned_labels(),
                    }
                },
            ),
            httpx.Response(200, json={}),
            httpx.Response(
                200,
                json={
                    "metadata": {
                        "name": "attached",
                        "labels": owned_labels(),
                        "deletionTimestamp": "2026-08-24T10:00:00Z",
                    }
                },
            ),
            httpx.Response(404),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        """Record both the initial deletion and its idempotent retry."""

        requests.append(request)
        return next(responses)

    config = ArgoCdConfig.from_settings(configured_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        first = client.delete_application(
            TEST_INSTANCE_ID,
            TEST_INSTANCE_SLUG,
            "attached",
        )
        second = client.delete_application(
            TEST_INSTANCE_ID,
            TEST_INSTANCE_SLUG,
            "attached",
        )

    assert first is ArgoCdMutationStatus.DEFERRED
    assert second is ArgoCdMutationStatus.COMPLETED
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/root/api/v1/applications/attached"),
        ("DELETE", "/root/api/v1/applications/attached"),
        ("GET", "/root/api/v1/applications/attached"),
        ("GET", "/root/api/v1/applications/attached"),
    ]
    assert [dict(request.url.params) for request in requests] == [
        {"project": "coder-project"},
        {
            "cascade": "true",
            "propagationPolicy": "foreground",
            "project": "coder-project",
        },
        {"project": "coder-project"},
        {"project": "coder-project"},
    ]
    assert requests[1].headers["content-type"] == "application/json"
    assert all(
        request.headers["authorization"] == "Bearer super-secret-token" for request in requests
    )
    assert requests[1].content == b""


def test_delete_application_completes_after_immediate_confirmed_absence() -> None:
    """Complete in one attempt when the post-delete observation is already 404."""

    responses = iter(
        (
            httpx.Response(
                200,
                json={
                    "metadata": {
                        "name": "attached",
                        "labels": owned_labels(),
                    }
                },
            ),
            httpx.Response(200, json={}),
            httpx.Response(404),
        )
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Expose disappearance immediately after accepting DELETE."""

        requests.append(request)
        return next(responses)

    config = ArgoCdConfig.from_settings(configured_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        result = client.delete_application(
            TEST_INSTANCE_ID,
            TEST_INSTANCE_SLUG,
            "attached",
        )

    assert result is ArgoCdMutationStatus.COMPLETED
    assert [request.method for request in requests] == ["GET", "DELETE", "GET"]


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
            _instance_id: UUID,
            _slug: str,
            _attached_name: str | None,
        ) -> str:
            """Return a sentinel status value."""

            return "status"

    monkeypatch.setattr(argocd_service, "ArgoCdClient", StubClient)

    result = argocd_service.read_instance_application_status(
        TEST_INSTANCE_ID,
        TEST_INSTANCE_SLUG,
        None,
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
                "metadata": {
                    "name": "attached",
                    "labels": owned_labels(),
                },
                "status": {"operationState": {"phase": phase}},
            },
        )

    config = ArgoCdConfig.from_settings(configured_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        result = client.delete_application(
            TEST_INSTANCE_ID,
            TEST_INSTANCE_SLUG,
            "attached",
        )

    assert result is ArgoCdMutationStatus.DEFERRED
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/root/api/v1/applications/attached")
    ]


def test_delete_instance_application_uses_process_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the configured client when the worker invokes the deletion service."""

    deleted: list[tuple[UUID, str, str | None]] = []

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
            instance_id: UUID,
            slug: str,
            attached_name: str | None,
        ) -> ArgoCdMutationStatus:
            """Capture the requested Application deletion."""

            deleted.append((instance_id, slug, attached_name))
            return ArgoCdMutationStatus.COMPLETED

    monkeypatch.setattr(argocd_service, "get_settings", configured_settings)
    monkeypatch.setattr(argocd_service, "ArgoCdClient", StubClient)

    result = argocd_service.delete_instance_application(
        TEST_INSTANCE_ID,
        TEST_INSTANCE_SLUG,
        "attached",
    )

    assert deleted == [(TEST_INSTANCE_ID, TEST_INSTANCE_SLUG, "attached")]
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
    """Resolve attached and deployment-prefix strict slug Application names."""

    config = ArgoCdClientConfig.from_settings(client_settings())

    assert application_name(config, TEST_INSTANCE_SLUG, "attached") == "attached"
    assert application_name(config, TEST_INSTANCE_SLUG, None) == TEST_APPLICATION_NAME


def test_client_configuration_uses_scalar_infrastructure_settings() -> None:
    """Expose one immutable deployment identity while redacting its token."""

    config = ArgoCdClientConfig.from_settings(client_settings())

    assert config.environment is Environment.DEVELOPMENT
    assert config.token == TEST_ARGOCD_TOKEN
    assert config.project == "coder-project"
    assert config.application_prefix == TEST_APPLICATION_PREFIX
    assert TEST_ARGOCD_TOKEN not in repr(config)


def test_one_client_uses_the_deployment_authorization() -> None:
    """Build every request with the deployment service-account token."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the strict Application existence request."""

        requests.append(request)
        return httpx.Response(404)

    config = ArgoCdClientConfig.from_settings(client_settings())
    with ArgoCdClient(config, transport=httpx.MockTransport(handler)) as client:
        assert not client.application_exists(TEST_INSTANCE_ID, TEST_INSTANCE_SLUG, None)

    assert [request.headers["authorization"] for request in requests] == [
        "Bearer super-secret-token"
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
            client_settings(argocd_application_prefix="x" * 51),
            "CODER_MANAGER_ARGOCD_APPLICATION_PREFIX",
        ),
        (
            client_settings(argocd_project_name=" "),
            "CODER_MANAGER_ARGOCD_PROJECT_NAME",
        ),
        (
            client_settings(environment=None),
            "CODER_MANAGER_ENVIRONMENT",
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


def test_scalar_token_is_required() -> None:
    """Reject an empty deployment bearer token."""

    settings = client_settings(argocd_token=" ")  # noqa: S106

    with pytest.raises(
        ArgoCdConfigurationError,
        match="CODER_MANAGER_ARGOCD_TOKEN",
    ):
        ArgoCdClientConfig.from_settings(settings)


@pytest.mark.parametrize("prefix", [" ", "x" * 51])
def test_scalar_application_prefix_is_required_and_valid(prefix: str) -> None:
    """Reject an empty or invalid deployment Application prefix."""

    settings = client_settings(argocd_application_prefix=prefix)

    with pytest.raises(
        ArgoCdConfigurationError,
        match="CODER_MANAGER_ARGOCD_APPLICATION_PREFIX",
    ):
        ArgoCdClientConfig.from_settings(settings)


def test_application_prefix_accepts_the_longest_slug_based_name() -> None:
    """Allow every prefix that keeps the generated Application name within 63 chars."""

    config = ArgoCdClientConfig.from_settings(client_settings(argocd_application_prefix="x" * 50))

    assert len(application_name(config, TEST_INSTANCE_SLUG, None)) == 63


def test_legacy_environment_specific_settings_are_not_supported() -> None:
    """Reject removed environment-specific settings without fallback."""

    settings = Settings.model_validate(
        {
            "environment": "development",
            "instance_base_domain": "emea.code-studio.dev.echonet",
            "argocd_url": "https://argocd.test",
            "argocd_development_token": "legacy-token",
            "argocd_development_application_prefix": "legacy-prefix",
            "argocd_development_project_name": "legacy-project",
        }
    )

    assert "argocd_development_token" not in Settings.model_fields
    assert "argocd_development_application_prefix" not in Settings.model_fields
    assert "argocd_development_project_name" not in Settings.model_fields
    with pytest.raises(
        ArgoCdConfigurationError,
        match="CODER_MANAGER_ARGOCD_TOKEN",
    ):
        ArgoCdClientConfig.from_settings(settings)


def test_legacy_global_project_setting_is_not_supported() -> None:
    """Require the renamed scalar project even when the old setting is supplied."""

    settings = client_settings(
        argocd_project_name=None,
        argocd_project="legacy-project",
    )

    assert "argocd_project" not in Settings.model_fields
    with pytest.raises(
        ArgoCdConfigurationError,
        match="CODER_MANAGER_ARGOCD_PROJECT_NAME",
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
            configured_settings(default_admins="alice\n--set global.identifier=evil"),
            "username with line breaks",
        ),
        (
            configured_settings(argocd_destination_name=" "),
            "CODER_MANAGER_ARGOCD_DESTINATION_NAME",
        ),
        (
            configured_settings(argocd_region=" "),
            "CODER_MANAGER_ARGOCD_REGION",
        ),
        (
            configured_settings(argocd_region="invalid_region"),
            "CODER_MANAGER_ARGOCD_REGION must be a valid DNS label",
        ),
        (
            configured_settings(cyberark_safe=" "),
            "CODER_MANAGER_CYBERARK_SAFE",
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
