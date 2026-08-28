"""Persistence operations for Coder templates."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coder_manager.models import (
    JobExecution,
    Template,
    TemplateAssignment,
    TemplateAssignmentStatus,
    TemplateSyncStatus,
    Workspace,
    WorkspaceStatus,
)
from coder_manager.repositories.job_executions import add_job_execution
from coder_manager.schemas import TemplateCreate, TemplateUpdate
from coder_manager.tasks.common.registry import TEMPLATE_SYNC_STEP_01, TEMPLATE_SYNC_STEP_01_TASK


class TemplateAlreadyExistsError(Exception):
    """Raised when a template name already exists globally."""


class TemplateNotFoundError(Exception):
    """Raised when a requested template does not exist."""


class TemplateHasAssignmentsError(Exception):
    """Raised when an assigned template cannot be deleted from the catalog."""


class TemplateWorkspaceCompatibilityError(Exception):
    """Raised when a template update would invalidate a workspace."""


class TemplateSyncInProgressError(Exception):
    """Raised when a mutation conflicts with an active template synchronization."""


class TemplateAssignmentsInProgressError(Exception):
    """Raised when template work conflicts with an active assignment transition."""


class TemplateWorkspacesInProgressError(Exception):
    """Raised when template work conflicts with a retryable workspace transition."""


class TemplateRepository:
    """Store and retrieve Coder templates using an async SQLAlchemy session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the database session used by repository operations."""

        self._session = session

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        display_name: str | None = None,
    ) -> tuple[list[Template], int]:
        """Return one deterministic filtered page and its matching total."""

        count_statement = select(func.count()).select_from(Template)
        list_statement = select(Template)

        if display_name is not None:
            display_name_condition = Template.display_name.icontains(
                display_name,
                autoescape=True,
            )
            count_statement = count_statement.where(display_name_condition)
            list_statement = list_statement.where(display_name_condition)

        total = await self._session.scalar(count_statement)
        result = await self._session.scalars(
            list_statement.order_by(
                func.lower(Template.display_name),
                Template.display_name,
                Template.name,
                Template.id,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(result), total or 0

    async def get(self, template_id: UUID) -> Template | None:
        """Find a template by its identifier."""

        return await self._session.get(Template, template_id)

    async def _discard_current_job(self, template: Template) -> None:
        """Remove the superseded synchronization job instead of retaining history."""

        if template.job_id is None:
            return
        job = await self._session.get(JobExecution, template.job_id)
        template.job_id = None
        await self._session.flush()
        if job is not None:
            await self._session.delete(job)

    async def create(self, payload: TemplateCreate) -> Template:
        """Create a validated template with one globally unique technical name."""

        template = Template(
            display_name=payload.display_name,
            name=payload.name,
            git_url=payload.git_url,
            source_path=payload.source_path,
            branch=payload.branch,
            modules=list(payload.modules),
        )
        self._session.add(template)
        try:
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise TemplateAlreadyExistsError from error
        await self._session.refresh(template)
        return template

    async def update(self, template_id: UUID, payload: TemplateUpdate) -> Template:
        """Replace mutable fields without invalidating attached workspaces."""

        # Lock the template so validation and replacement observe one stable version.
        template = await self._session.scalar(
            select(Template).where(Template.id == template_id).with_for_update()
        )
        if template is None:
            await self._session.rollback()
            raise TemplateNotFoundError
        if template.sync_status in {
            TemplateSyncStatus.PENDING,
            TemplateSyncStatus.RUNNING,
            TemplateSyncStatus.ERROR,
        }:
            await self._session.rollback()
            raise TemplateSyncInProgressError
        await self._ensure_no_active_assignments(template.id)
        await self._ensure_no_active_workspaces(template.id)

        # Preserve an unchanged template without touching its update timestamp.
        changed = (
            template.display_name != payload.display_name
            or template.git_url != payload.git_url
            or template.source_path != payload.source_path
            or template.branch != payload.branch
            or template.modules != payload.modules
        )
        if not changed:
            await self._session.commit()
            return template

        # Reject module changes that would invalidate an existing workspace.
        workspaces = await self._session.scalars(
            select(Workspace).where(Workspace.template_id == template.id)
        )
        allowed_modules = set(payload.modules)
        for workspace in workspaces:
            if not set(workspace.modules).issubset(allowed_modules):
                await self._session.rollback()
                raise TemplateWorkspaceCompatibilityError

        # Apply all mutable fields only after every dependent workspace passes validation.
        template.display_name = payload.display_name
        template.git_url = payload.git_url
        template.source_path = payload.source_path
        template.branch = payload.branch
        template.modules = list(payload.modules)
        await self._discard_current_job(template)
        template.action = "updated"
        template.sync_status = TemplateSyncStatus.SUCCESS
        template.step = None
        template.updated_at = datetime.now(UTC)
        await self._session.commit()
        await self._session.refresh(template)
        return template

    async def delete(self, template_id: UUID) -> None:
        """Delete one template or raise when it does not exist."""

        template = await self._session.scalar(
            select(Template).where(Template.id == template_id).with_for_update()
        )
        if template is None:
            await self._session.rollback()
            raise TemplateNotFoundError
        if template.sync_status in {
            TemplateSyncStatus.PENDING,
            TemplateSyncStatus.RUNNING,
            TemplateSyncStatus.ERROR,
        }:
            await self._session.rollback()
            raise TemplateSyncInProgressError
        assignment_id = await self._session.scalar(
            select(TemplateAssignment.id)
            .where(TemplateAssignment.template_id == template_id)
            .limit(1)
        )
        if assignment_id is not None:
            await self._session.rollback()
            raise TemplateHasAssignmentsError
        await self._discard_current_job(template)
        await self._session.delete(template)
        await self._session.commit()

    async def request_sync(self, template_id: UUID) -> UUID:
        """Create one durable fire-and-forget synchronization job."""

        template = await self._session.scalar(
            select(Template).where(Template.id == template_id).with_for_update()
        )
        if template is None:
            await self._session.rollback()
            raise TemplateNotFoundError
        if template.sync_status in {
            TemplateSyncStatus.PENDING,
            TemplateSyncStatus.RUNNING,
            TemplateSyncStatus.ERROR,
        }:
            await self._session.rollback()
            raise TemplateSyncInProgressError
        await self._ensure_no_active_assignments(template.id)
        await self._ensure_no_active_workspaces(template.id)

        await self._discard_current_job(template)
        job = add_job_execution(
            self._session,
            name="template.sync",
            task_name=TEMPLATE_SYNC_STEP_01_TASK,
            resource_type="template",
            resource_id=template.id,
            step=TEMPLATE_SYNC_STEP_01,
        )
        template.action = "syncing"
        template.sync_status = TemplateSyncStatus.PENDING
        template.job_id = job.id
        template.step = TEMPLATE_SYNC_STEP_01
        await self._session.commit()
        return job.id

    async def _ensure_no_active_assignments(self, template_id: UUID) -> None:
        """Reject template work while an assignment transition remains retryable."""

        assignment_id = await self._session.scalar(
            select(TemplateAssignment.id)
            .where(
                TemplateAssignment.template_id == template_id,
                TemplateAssignment.status.in_(
                    [
                        TemplateAssignmentStatus.PENDING,
                        TemplateAssignmentStatus.RUNNING,
                        TemplateAssignmentStatus.ERROR,
                    ]
                ),
            )
            .limit(1)
        )
        if assignment_id is not None:
            await self._session.rollback()
            raise TemplateAssignmentsInProgressError

    async def _ensure_no_active_workspaces(self, template_id: UUID) -> None:
        """Reject synchronization while a retryable workspace mutation owns the template."""

        workspace_id = await self._session.scalar(
            select(Workspace.id)
            .where(
                Workspace.template_id == template_id,
                Workspace.status.in_(
                    [WorkspaceStatus.PENDING, WorkspaceStatus.RUNNING, WorkspaceStatus.ERROR]
                ),
            )
            .limit(1)
        )
        if workspace_id is not None:
            await self._session.rollback()
            raise TemplateWorkspacesInProgressError
