"""Persistence and lifecycle operations for Coder workspaces."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from coder_manager.models import (
    Instance,
    InstanceStatus,
    Member,
    MemberStatus,
    Template,
    TemplateImage,
    TemplateScope,
    Workspace,
    WorkspaceStatus,
)
from coder_manager.repositories.job_executions import add_job_execution
from coder_manager.tasks.common.registry import (
    WORKSPACE_CREATE_STEP_01,
    WORKSPACE_CREATE_STEP_01_TASK,
    WORKSPACE_DELETE_STEP_01,
    WORKSPACE_DELETE_STEP_01_TASK,
    WORKSPACE_UPDATE_STEP_01,
    WORKSPACE_UPDATE_STEP_01_TASK,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from coder_manager.schemas import WorkspaceCreate, WorkspaceListQuery, WorkspaceUpdate

MAX_ACTION_LENGTH = 255


class WorkspaceAlreadyExistsError(Exception):
    """Raised when a workspace name already exists in an instance."""


class WorkspaceNotFoundError(Exception):
    """Raised when a workspace does not exist."""


class WorkspaceInstanceNotFoundError(Exception):
    """Raised when a workspace references an unknown instance."""


class WorkspaceInstanceBusyError(Exception):
    """Raised when an instance action prevents workspace mutations."""


class WorkspaceTemplateNotFoundError(Exception):
    """Raised when a workspace references an unknown template."""


class WorkspaceTemplateUnavailableError(Exception):
    """Raised when a template is unavailable to the workspace instance."""


class WorkspaceMemberNotFoundError(Exception):
    """Raised when a workspace references an unknown member."""


class WorkspaceMemberUnavailableError(Exception):
    """Raised when an owner is not ready or belongs to another instance."""


class WorkspaceImageNotFoundError(Exception):
    """Raised when a workspace references an unknown image."""


class WorkspaceImageUnavailableError(Exception):
    """Raised when an image is not allowed by the workspace template."""


class WorkspaceConfigurationError(Exception):
    """Raised when modules or resources violate the template contract."""


class WorkspaceBusyError(Exception):
    """Raised when a workspace action is already in progress."""


class WorkspaceActionConflictError(Exception):
    """Raised when an internal workspace result is stale."""


class InvalidWorkspaceActionError(Exception):
    """Raised when an internal workspace action is empty or too long."""


class WorkspaceRepository:
    """Store and transition workspaces using an async SQLAlchemy session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the database session used by repository operations."""

        self._session = session

    async def list_page(self, query: WorkspaceListQuery) -> tuple[list[Workspace], int]:
        """Return one deterministic filtered workspace page and total."""

        count_statement = select(func.count()).select_from(Workspace)
        list_statement = select(Workspace)
        filters = (
            (Workspace.instance_id, query.instance_id),
            (Workspace.template_id, query.template_id),
            (Workspace.member_id, query.member_id),
            (Workspace.image_id, query.image_id),
            (Workspace.status, query.status),
        )
        for column, value in filters:
            if value is not None:
                count_statement = count_statement.where(column == value)
                list_statement = list_statement.where(column == value)
        if query.name is not None:
            condition = Workspace.name.icontains(query.name, autoescape=True)
            count_statement = count_statement.where(condition)
            list_statement = list_statement.where(condition)

        total = await self._session.scalar(count_statement)
        result = await self._session.scalars(
            list_statement.order_by(
                Workspace.instance_id,
                func.lower(Workspace.name),
                Workspace.name,
                Workspace.id,
            )
            .offset((query.page - 1) * query.page_size)
            .limit(query.page_size)
        )
        return list(result), total or 0

    async def get(self, workspace_id: UUID) -> Workspace | None:
        """Find a workspace by its identifier."""

        return await self._session.get(Workspace, workspace_id)

    async def create(self, payload: WorkspaceCreate) -> Workspace:
        """Create a validated workspace in creating/pending state."""

        # Lock and validate every referenced record before constructing the workspace.
        instance = await self._lock_available_instance(payload.instance_id)
        template = await self._lock_template(payload.template_id)
        await self._validate_template_scope(template, instance)
        await self._validate_owner(payload.member_id, instance.id)
        await self._validate_image(payload.image_id, template.id)
        await self._validate_configuration(
            template,
            modules=payload.modules,
            cpu=payload.cpu,
            ram=payload.ram,
            disk=payload.disk,
        )

        # Persist the validated configuration as one pending lifecycle operation.
        workspace_id = uuid4()
        workspace = Workspace(
            id=workspace_id,
            name=payload.name,
            instance_id=instance.id,
            template_id=template.id,
            member_id=payload.member_id,
            image_id=payload.image_id,
            modules=list(payload.modules),
            cpu=payload.cpu,
            ram=payload.ram,
            disk=payload.disk,
            action="creating",
            status=WorkspaceStatus.PENDING,
            step=WORKSPACE_CREATE_STEP_01,
        )
        job = add_job_execution(
            self._session,
            name="workspace.create",
            task_name=WORKSPACE_CREATE_STEP_01_TASK,
            resource_type="workspace",
            resource_id=workspace_id,
            step=WORKSPACE_CREATE_STEP_01,
        )
        workspace.job_id = job.id
        self._session.add(workspace)
        try:
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise WorkspaceAlreadyExistsError from error
        await self._session.refresh(workspace)
        return workspace

    async def update(self, workspace_id: UUID, payload: WorkspaceUpdate) -> tuple[Workspace, bool]:
        """Replace mutable fields after revalidating the complete configuration."""

        # Revalidate parents and template limits under row locks before comparing fields.
        workspace = await self._lock_workspace(workspace_id)
        await self._ensure_workspace_available(workspace)
        await self._lock_available_instance(workspace.instance_id)
        template = await self._lock_template(workspace.template_id)
        await self._validate_image(payload.image_id, template.id)
        await self._validate_configuration(
            template,
            modules=payload.modules,
            cpu=payload.cpu,
            ram=payload.ram,
            disk=workspace.disk,
        )

        # Avoid scheduling a worker when the requested state already matches a success.
        changed = (
            workspace.name != payload.name
            or workspace.image_id != payload.image_id
            or workspace.modules != payload.modules
            or workspace.cpu != payload.cpu
            or workspace.ram != payload.ram
        )
        if not changed and workspace.status is WorkspaceStatus.SUCCESS:
            await self._session.commit()
            return workspace, False

        # Store the new desired state and let the background worker reconcile it.
        workspace.name = payload.name
        workspace.image_id = payload.image_id
        workspace.modules = list(payload.modules)
        workspace.cpu = payload.cpu
        workspace.ram = payload.ram
        workspace.action = "updating"
        workspace.status = WorkspaceStatus.PENDING
        job = add_job_execution(
            self._session,
            name="workspace.update",
            task_name=WORKSPACE_UPDATE_STEP_01_TASK,
            resource_type="workspace",
            resource_id=workspace.id,
            step=WORKSPACE_UPDATE_STEP_01,
        )
        workspace.job_id = job.id
        workspace.step = WORKSPACE_UPDATE_STEP_01
        workspace.updated_at = datetime.now(UTC)
        try:
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise WorkspaceAlreadyExistsError from error
        await self._session.refresh(workspace)
        return workspace, True

    async def request_deletion(self, workspace_id: UUID) -> Workspace:
        """Move an available workspace to deleting/pending."""

        workspace = await self._lock_workspace(workspace_id)
        await self._ensure_workspace_available(workspace)
        await self._lock_available_instance(workspace.instance_id)
        workspace.action = "deleting"
        workspace.status = WorkspaceStatus.PENDING
        job = add_job_execution(
            self._session,
            name="workspace.delete",
            task_name=WORKSPACE_DELETE_STEP_01_TASK,
            resource_type="workspace",
            resource_id=workspace.id,
            step=WORKSPACE_DELETE_STEP_01,
        )
        workspace.job_id = job.id
        workspace.step = WORKSPACE_DELETE_STEP_01
        workspace.updated_at = datetime.now(UTC)
        await self._session.commit()
        await self._session.refresh(workspace)
        return workspace

    async def update_action(
        self,
        workspace_id: UUID,
        *,
        expected_action: str,
        action: str,
        status: WorkspaceStatus,
    ) -> Workspace:
        """Update action state while rejecting results from a stale action."""

        normalized_action = action.strip()
        if not normalized_action or len(normalized_action) > MAX_ACTION_LENGTH:
            raise InvalidWorkspaceActionError
        workspace = await self._session.scalar(
            select(Workspace).where(Workspace.id == workspace_id).with_for_update()
        )
        if workspace is None:
            await self._session.rollback()
            raise WorkspaceNotFoundError
        if workspace.action != expected_action:
            await self._session.rollback()
            raise WorkspaceActionConflictError
        workspace.action = normalized_action
        workspace.status = status
        workspace.updated_at = datetime.now(UTC)
        await self._session.commit()
        await self._session.refresh(workspace)
        return workspace

    async def _lock_available_instance(self, instance_id: UUID) -> Instance:
        """Lock an instance and reject missing or in-progress parents."""

        instance = await self._session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if instance is None:
            await self._session.rollback()
            raise WorkspaceInstanceNotFoundError
        if instance.status is not InstanceStatus.SUCCESS:
            await self._session.rollback()
            raise WorkspaceInstanceBusyError
        return instance

    async def _lock_template(self, template_id: UUID) -> Template:
        """Lock and return a template or reject an unknown identifier."""

        template = await self._session.scalar(
            select(Template).where(Template.id == template_id).with_for_update()
        )
        if template is None:
            await self._session.rollback()
            raise WorkspaceTemplateNotFoundError
        return template

    async def _validate_owner(self, member_id: UUID, instance_id: UUID) -> None:
        """Ensure the locked owner is ready and belongs to the target instance."""

        member = await self._session.scalar(
            select(Member).where(Member.id == member_id).with_for_update()
        )
        if member is None:
            await self._session.rollback()
            raise WorkspaceMemberNotFoundError
        if member.instance_id != instance_id or member.status is not MemberStatus.SUCCESS:
            await self._session.rollback()
            raise WorkspaceMemberUnavailableError

    async def _validate_image(self, image_id: UUID, template_id: UUID) -> None:
        """Ensure the locked image is allowed by the selected template."""

        image = await self._session.scalar(
            select(TemplateImage).where(TemplateImage.id == image_id).with_for_update()
        )
        if image is None:
            await self._session.rollback()
            raise WorkspaceImageNotFoundError
        if image.template_id != template_id:
            await self._session.rollback()
            raise WorkspaceImageUnavailableError

    async def _lock_workspace(self, workspace_id: UUID) -> Workspace:
        """Lock and return a workspace or reject an unknown identifier."""

        workspace = await self._session.scalar(
            select(Workspace).where(Workspace.id == workspace_id).with_for_update()
        )
        if workspace is None:
            await self._session.rollback()
            raise WorkspaceNotFoundError
        return workspace

    async def _ensure_workspace_available(self, workspace: Workspace) -> None:
        """Reject a workspace while another lifecycle action is in progress."""

        if workspace.status in {WorkspaceStatus.PENDING, WorkspaceStatus.RUNNING}:
            await self._session.rollback()
            raise WorkspaceBusyError

    async def _validate_template_scope(self, template: Template, instance: Instance) -> None:
        """Ensure an application-scoped template belongs to the instance application."""

        if (
            template.scope is TemplateScope.APPLICATION
            and template.application != instance.application
        ):
            await self._session.rollback()
            raise WorkspaceTemplateUnavailableError

    async def _validate_configuration(
        self,
        template: Template,
        *,
        modules: Sequence[str],
        cpu: int,
        ram: int,
        disk: int,
    ) -> None:
        """Ensure requested resources and modules remain within template limits."""

        resources_valid = (
            template.min_cpu_count <= cpu <= template.max_cpu_count
            and template.min_ram_gb <= ram <= template.max_ram_gb
            and template.min_disk_gb <= disk <= template.max_disk_gb
        )
        modules_valid = set(modules).issubset(set(template.modules))
        if not resources_valid or not modules_valid:
            await self._session.rollback()
            raise WorkspaceConfigurationError
