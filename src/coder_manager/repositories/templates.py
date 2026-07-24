"""Persistence operations for Coder templates."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coder_manager.models import (
    JobExecution,
    Template,
    TemplateScope,
    TemplateSyncStatus,
    Workspace,
)
from coder_manager.repositories.job_executions import add_job_execution
from coder_manager.schemas import TemplateCreate, TemplateUpdate
from coder_manager.tasks.common.registry import TEMPLATE_SYNC_STEP_01, TEMPLATE_SYNC_STEP_01_TASK


class TemplateAlreadyExistsError(Exception):
    """Raised when a template name already exists in the target scope."""


class TemplateNotFoundError(Exception):
    """Raised when a requested template does not exist."""


class TemplateHasWorkspacesError(Exception):
    """Raised when a referenced template cannot be deleted."""


class TemplateWorkspaceCompatibilityError(Exception):
    """Raised when a template update would invalidate a workspace."""


class TemplateSyncInProgressError(Exception):
    """Raised when a mutation conflicts with an active template synchronization."""


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
        scope: TemplateScope | None = None,
        application: str | None = None,
        name: str | None = None,
    ) -> tuple[list[Template], int]:
        """Return one deterministic filtered page and its matching total."""

        count_statement = select(func.count()).select_from(Template)
        list_statement = select(Template)

        if application is not None:
            available_to_application = or_(
                Template.scope == TemplateScope.GLOBAL,
                and_(
                    Template.scope == TemplateScope.APPLICATION,
                    Template.application == application,
                ),
            )
            count_statement = count_statement.where(available_to_application)
            list_statement = list_statement.where(available_to_application)
        if scope is not None:
            scope_condition = Template.scope == scope
            count_statement = count_statement.where(scope_condition)
            list_statement = list_statement.where(scope_condition)
        if name is not None:
            name_condition = Template.name.icontains(name, autoescape=True)
            count_statement = count_statement.where(name_condition)
            list_statement = list_statement.where(name_condition)

        total = await self._session.scalar(count_statement)
        result = await self._session.scalars(
            list_statement.order_by(
                func.lower(Template.name),
                Template.name,
                Template.scope,
                Template.application,
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
        """Create a validated global or application-scoped template."""

        template = Template(
            name=payload.name,
            scope=payload.scope,
            application=payload.application,
            coder_name=payload.coder_name,
            git_url=payload.git_url,
            source_path=payload.source_path,
            branch=payload.branch,
            modules=list(payload.modules),
            min_cpu_count=payload.min_cpu_count,
            max_cpu_count=payload.max_cpu_count,
            min_ram_gb=payload.min_ram_gb,
            max_ram_gb=payload.max_ram_gb,
            min_disk_gb=payload.min_disk_gb,
            max_disk_gb=payload.max_disk_gb,
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
        if template.sync_status in {TemplateSyncStatus.PENDING, TemplateSyncStatus.RUNNING}:
            await self._session.rollback()
            raise TemplateSyncInProgressError

        # Preserve an unchanged template without touching its update timestamp.
        changed = (
            template.name != payload.name
            or template.git_url != payload.git_url
            or template.source_path != payload.source_path
            or template.branch != payload.branch
            or template.modules != payload.modules
            or template.min_cpu_count != payload.min_cpu_count
            or template.max_cpu_count != payload.max_cpu_count
            or template.min_ram_gb != payload.min_ram_gb
            or template.max_ram_gb != payload.max_ram_gb
            or template.min_disk_gb != payload.min_disk_gb
            or template.max_disk_gb != payload.max_disk_gb
        )
        if not changed:
            await self._session.commit()
            return template

        # Reject ranges or modules that would invalidate an existing workspace.
        workspaces = await self._session.scalars(
            select(Workspace).where(Workspace.template_id == template.id)
        )
        allowed_modules = set(payload.modules)
        for workspace in workspaces:
            resources_valid = (
                payload.min_cpu_count <= workspace.cpu <= payload.max_cpu_count
                and payload.min_ram_gb <= workspace.ram <= payload.max_ram_gb
                and payload.min_disk_gb <= workspace.disk <= payload.max_disk_gb
            )
            if not resources_valid or not set(workspace.modules).issubset(allowed_modules):
                await self._session.rollback()
                raise TemplateWorkspaceCompatibilityError

        # Apply all mutable fields only after every dependent workspace passes validation.
        template.name = payload.name
        template.git_url = payload.git_url
        template.source_path = payload.source_path
        template.branch = payload.branch
        template.modules = list(payload.modules)
        template.min_cpu_count = payload.min_cpu_count
        template.max_cpu_count = payload.max_cpu_count
        template.min_ram_gb = payload.min_ram_gb
        template.max_ram_gb = payload.max_ram_gb
        template.min_disk_gb = payload.min_disk_gb
        template.max_disk_gb = payload.max_disk_gb
        await self._discard_current_job(template)
        template.action = "updated"
        template.sync_status = TemplateSyncStatus.SUCCESS
        template.step = None
        template.updated_at = datetime.now(UTC)
        try:
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise TemplateAlreadyExistsError from error
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
        if template.sync_status in {TemplateSyncStatus.PENDING, TemplateSyncStatus.RUNNING}:
            await self._session.rollback()
            raise TemplateSyncInProgressError
        workspace_id = await self._session.scalar(
            select(Workspace.id).where(Workspace.template_id == template_id).limit(1)
        )
        if workspace_id is not None:
            await self._session.rollback()
            raise TemplateHasWorkspacesError
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
        if template.sync_status in {TemplateSyncStatus.PENDING, TemplateSyncStatus.RUNNING}:
            await self._session.rollback()
            raise TemplateSyncInProgressError

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
