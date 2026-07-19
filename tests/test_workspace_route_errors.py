"""Isolated error mappings for workspace-related API routes."""

from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, Response

from coder_manager.api.routes import template_images as image_routes
from coder_manager.api.routes import templates as template_routes
from coder_manager.api.routes import workspaces as workspace_routes
from coder_manager.repositories import (
    TemplateAlreadyExistsError,
    TemplateApplicationNotFoundError,
    TemplateHasWorkspacesError,
    TemplateImageAlreadyExistsError,
    TemplateImageInUseError,
    TemplateImageNotFoundError,
    TemplateImageTemplateNotFoundError,
    TemplateNotFoundError,
    TemplateWorkspaceCompatibilityError,
    WorkspaceAlreadyExistsError,
    WorkspaceBusyError,
    WorkspaceConfigurationError,
    WorkspaceImageNotFoundError,
    WorkspaceImageUnavailableError,
    WorkspaceInstanceBusyError,
    WorkspaceInstanceNotFoundError,
    WorkspaceMemberNotFoundError,
    WorkspaceMemberUnavailableError,
    WorkspaceNotFoundError,
    WorkspaceTemplateNotFoundError,
    WorkspaceTemplateUnavailableError,
)
from coder_manager.schemas import (
    TemplateCreate,
    TemplateImageCreate,
    TemplateUpdate,
    WorkspaceCreate,
    WorkspaceUpdate,
)


def template_create_payload() -> TemplateCreate:
    """Build a valid template creation schema."""

    return TemplateCreate(
        name="Python",
        scope="global",
        application_id=None,
        git_url="https://git.example.com/template.git",
        modules=["code-server"],
        version="v1",
        min_cpu_count=1,
        max_cpu_count=8,
        min_ram_gb=2,
        max_ram_gb=32,
        min_disk_gb=10,
        max_disk_gb=100,
    )


def template_update_payload() -> TemplateUpdate:
    """Build a valid template update schema."""

    create = template_create_payload()
    return TemplateUpdate.model_validate(create.model_dump(exclude={"scope", "application_id"}))


def workspace_create_payload() -> WorkspaceCreate:
    """Build a syntactically valid workspace creation schema."""

    return WorkspaceCreate(
        name="development",
        instance_id=uuid4(),
        template_id=uuid4(),
        member_id=uuid4(),
        image_id=uuid4(),
        modules=[],
        cpu=2,
        ram=8,
        disk=20,
    )


def workspace_update_payload() -> WorkspaceUpdate:
    """Build a syntactically valid workspace update schema."""

    return WorkspaceUpdate(
        name="development",
        image_id=uuid4(),
        modules=[],
        cpu=2,
        ram=8,
    )


@pytest.mark.parametrize(
    ("repository_error", "expected_status"),
    [
        (WorkspaceInstanceNotFoundError, 404),
        (WorkspaceTemplateNotFoundError, 404),
        (WorkspaceMemberNotFoundError, 404),
        (WorkspaceImageNotFoundError, 404),
        (WorkspaceInstanceBusyError, 409),
        (WorkspaceMemberUnavailableError, 409),
        (WorkspaceTemplateUnavailableError, 422),
        (WorkspaceImageUnavailableError, 422),
        (WorkspaceConfigurationError, 422),
        (WorkspaceAlreadyExistsError, 409),
    ],
)
async def test_create_workspace_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
    repository_error: type[Exception],
    expected_status: int,
) -> None:
    """Verify the create workspace error mapping scenario."""

    class FailingRepository:
        """Provide the failing repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def create(self, _payload: WorkspaceCreate) -> None:
            """Simulate the repository create operation."""

            raise repository_error

    monkeypatch.setattr(workspace_routes, "WorkspaceRepository", FailingRepository)
    with pytest.raises(HTTPException) as caught:
        await workspace_routes.create_workspace(workspace_create_payload(), None)
    assert caught.value.status_code == expected_status


@pytest.mark.parametrize(
    ("repository_error", "expected_status"),
    [
        (WorkspaceNotFoundError, 404),
        (WorkspaceInstanceNotFoundError, 404),
        (WorkspaceTemplateNotFoundError, 404),
        (WorkspaceImageNotFoundError, 404),
        (WorkspaceInstanceBusyError, 409),
        (WorkspaceBusyError, 409),
        (WorkspaceImageUnavailableError, 422),
        (WorkspaceConfigurationError, 422),
        (WorkspaceAlreadyExistsError, 409),
    ],
)
async def test_update_workspace_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
    repository_error: type[Exception],
    expected_status: int,
) -> None:
    """Verify the update workspace error mapping scenario."""

    class FailingRepository:
        """Provide the failing repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def update(self, _workspace_id: UUID, _payload: WorkspaceUpdate) -> None:
            """Simulate the repository update operation."""

            raise repository_error

    monkeypatch.setattr(workspace_routes, "WorkspaceRepository", FailingRepository)
    with pytest.raises(HTTPException) as caught:
        await workspace_routes.update_workspace(
            uuid4(), workspace_update_payload(), None, Response()
        )
    assert caught.value.status_code == expected_status


@pytest.mark.parametrize(
    ("repository_error", "expected_status"),
    [
        (WorkspaceNotFoundError, 404),
        (WorkspaceInstanceNotFoundError, 404),
        (WorkspaceInstanceBusyError, 409),
        (WorkspaceBusyError, 409),
    ],
)
async def test_delete_workspace_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
    repository_error: type[Exception],
    expected_status: int,
) -> None:
    """Verify the delete workspace error mapping scenario."""

    class FailingRepository:
        """Provide the failing repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def request_deletion(self, _workspace_id: UUID) -> None:
            """Simulate the repository request deletion operation."""

            raise repository_error

    monkeypatch.setattr(workspace_routes, "WorkspaceRepository", FailingRepository)
    with pytest.raises(HTTPException) as caught:
        await workspace_routes.delete_workspace(uuid4(), None)
    assert caught.value.status_code == expected_status


@pytest.mark.parametrize(
    ("method", "repository_error", "expected_status"),
    [
        ("list", TemplateImageTemplateNotFoundError, 404),
        ("create", TemplateImageTemplateNotFoundError, 404),
        ("create", TemplateImageAlreadyExistsError, 409),
        ("delete", TemplateImageTemplateNotFoundError, 404),
        ("delete", TemplateImageNotFoundError, 404),
        ("delete", TemplateImageInUseError, 409),
    ],
)
async def test_template_image_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    repository_error: type[Exception],
    expected_status: int,
) -> None:
    """Verify the template image error mapping scenario."""

    class FailingRepository:
        """Provide the failing repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def list(self, *_args: object, **_kwargs: object) -> None:
            """Simulate the repository list operation."""

            raise repository_error

        async def create(self, *_args: object) -> None:
            """Simulate the repository create operation."""

            raise repository_error

        async def delete(self, *_args: object) -> None:
            """Simulate the repository delete operation."""

            raise repository_error

    monkeypatch.setattr(image_routes, "TemplateImageRepository", FailingRepository)

    async def invoke_route() -> None:
        """Simulate the invoke route operation used by this scenario."""

        if method == "list":
            await image_routes.list_template_images(uuid4(), None, 1, 20)
        elif method == "create":
            payload = TemplateImageCreate(registry="docker.io", name="python", version="3")
            await image_routes.create_template_image(uuid4(), payload, None)
        else:
            await image_routes.delete_template_image(uuid4(), uuid4(), None)

    with pytest.raises(HTTPException) as caught:
        await invoke_route()
    assert caught.value.status_code == expected_status


@pytest.mark.parametrize(
    ("method", "repository_error", "expected_status"),
    [
        ("create", TemplateApplicationNotFoundError, 404),
        ("create", TemplateAlreadyExistsError, 409),
        ("update", TemplateNotFoundError, 404),
        ("update", TemplateAlreadyExistsError, 409),
        ("update", TemplateWorkspaceCompatibilityError, 409),
        ("delete", TemplateNotFoundError, 404),
        ("delete", TemplateHasWorkspacesError, 409),
    ],
)
async def test_template_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    repository_error: type[Exception],
    expected_status: int,
) -> None:
    """Verify the template error mapping scenario."""

    class FailingRepository:
        """Provide the failing repository test double for this scenario."""

        def __init__(self, _session: object) -> None:
            """Initialize the test double used by this scenario."""

        async def create(self, *_args: object) -> None:
            """Simulate the repository create operation."""

            raise repository_error

        async def update(self, *_args: object) -> None:
            """Simulate the repository update operation."""

            raise repository_error

        async def delete(self, *_args: object) -> None:
            """Simulate the repository delete operation."""

            raise repository_error

    monkeypatch.setattr(template_routes, "TemplateRepository", FailingRepository)

    async def invoke_route() -> None:
        """Simulate the invoke route operation used by this scenario."""

        if method == "create":
            await template_routes.create_template(template_create_payload(), None)
        elif method == "update":
            await template_routes.update_template(uuid4(), template_update_payload(), None)
        else:
            await template_routes.delete_template(uuid4(), None)

    with pytest.raises(HTTPException) as caught:
        await invoke_route()
    assert caught.value.status_code == expected_status
