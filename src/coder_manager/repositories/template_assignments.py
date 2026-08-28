"""Persistence operations for explicit instance template assignments."""

from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from coder_manager.models import (
    Instance,
    InstanceState,
    InstanceStatus,
    JobExecution,
    Template,
    TemplateAssignment,
    TemplateAssignmentStatus,
    TemplateSyncStatus,
    Workspace,
    WorkspaceStatus,
)
from coder_manager.repositories.job_executions import add_job_execution
from coder_manager.tasks.common.registry import (
    TEMPLATE_ASSIGNMENT_CREATE_STEP_01,
    TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
    TEMPLATE_ASSIGNMENT_DELETE_STEP_01,
    TEMPLATE_ASSIGNMENT_DELETE_STEP_01_TASK,
)


class TemplateAssignmentInstanceNotFoundError(Exception):
    """Raised when an assignment operation references an unknown instance."""


class TemplateAssignmentTemplateNotFoundError(Exception):
    """Raised when an assignment operation references an unknown template."""


class TemplateAssignmentInstanceUnavailableError(Exception):
    """Raised when an instance cannot accept a template assignment mutation."""


class TemplateAssignmentActionConflictError(Exception):
    """Raised when the requested transition conflicts with the current action."""


class TemplateAssignmentJobConflictError(Exception):
    """Raised when an active assignment does not own a consistent durable job."""


class TemplateAssignmentSyncInProgressError(Exception):
    """Raised when a template-wide synchronization conflicts with an assignment mutation."""


class TemplateAssignmentWorkspacesBusyError(Exception):
    """Raised when local workspaces are still running an action for an assignment."""


class TemplateAssignmentRepository:
    """Store and transition explicit template assignments transactionally."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the database session used by repository operations."""

        self._session = session

    async def list(
        self,
        instance_id: UUID,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[TemplateAssignment], int]:
        """Return one deterministic page after validating the parent instance."""

        if await self._session.get(Instance, instance_id) is None:
            raise TemplateAssignmentInstanceNotFoundError

        assignment_filter = TemplateAssignment.instance_id == instance_id
        total = await self._session.scalar(
            select(func.count()).select_from(TemplateAssignment).where(assignment_filter)
        )
        assignments = await self._session.scalars(
            select(TemplateAssignment)
            .join(Template, Template.id == TemplateAssignment.template_id)
            .where(assignment_filter)
            .options(
                selectinload(TemplateAssignment.template),
                selectinload(TemplateAssignment.deployment),
            )
            .order_by(
                func.lower(Template.display_name),
                Template.display_name,
                Template.name,
                TemplateAssignment.id,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(assignments), total or 0

    async def put(
        self,
        instance_id: UUID,
        template_id: UUID,
    ) -> tuple[TemplateAssignment, bool]:
        """Create an assignment or return its idempotent current state.

        The boolean reports whether the request remains asynchronously accepted. A newly
        created or retryable in-progress assignment returns true; an already converged
        assignment returns false.
        """

        instance = await self._lock_instance(instance_id)
        template = await self._lock_template(template_id)
        await self._require_mutation_available(instance, template)
        assignment = await self._lock_assignment(instance_id, template_id)

        if assignment is None:
            assignment_id = uuid4()
            assignment = TemplateAssignment(
                id=assignment_id,
                instance_id=instance_id,
                template_id=template_id,
                action="creating",
                status=TemplateAssignmentStatus.PENDING,
                step=TEMPLATE_ASSIGNMENT_CREATE_STEP_01,
            )
            job = add_job_execution(
                self._session,
                name="template_assignment.create",
                task_name=TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
                resource_type="template_assignment",
                resource_id=assignment_id,
                step=TEMPLATE_ASSIGNMENT_CREATE_STEP_01,
            )
            assignment.job_id = job.id
            self._session.add(assignment)
            self._session.info["enqueue_job_id"] = job.id
            await self._session.commit()
            return await self._stored_assignment(assignment_id), True

        if assignment.action == "created" and assignment.status is TemplateAssignmentStatus.SUCCESS:
            await self._session.commit()
            return assignment, False
        if assignment.action == "creating" and assignment.status in {
            TemplateAssignmentStatus.PENDING,
            TemplateAssignmentStatus.RUNNING,
            TemplateAssignmentStatus.ERROR,
        }:
            await self._require_consistent_job(
                assignment,
                job_name="template_assignment.create",
                task_name=TEMPLATE_ASSIGNMENT_CREATE_STEP_01_TASK,
                step=TEMPLATE_ASSIGNMENT_CREATE_STEP_01,
            )
            await self._session.commit()
            return assignment, True

        await self._session.rollback()
        raise TemplateAssignmentActionConflictError

    async def request_deletion(
        self,
        instance_id: UUID,
        template_id: UUID,
    ) -> tuple[TemplateAssignment | None, bool]:
        """Request removal or return the idempotent current deletion state.

        A missing assignment is already removed and returns ``(None, False)``. Any returned
        assignment remains asynchronously accepted and therefore returns true.
        """

        instance = await self._lock_instance(instance_id)
        template = await self._lock_template(template_id)
        assignment = await self._lock_assignment(instance_id, template_id)
        if assignment is None:
            await self._session.commit()
            return None, False

        await self._require_mutation_available(instance, template)
        if assignment.action == "deleting" and assignment.status in {
            TemplateAssignmentStatus.PENDING,
            TemplateAssignmentStatus.RUNNING,
            TemplateAssignmentStatus.ERROR,
        }:
            await self._require_consistent_job(
                assignment,
                job_name="template_assignment.delete",
                task_name=TEMPLATE_ASSIGNMENT_DELETE_STEP_01_TASK,
                step=TEMPLATE_ASSIGNMENT_DELETE_STEP_01,
            )
            await self._session.commit()
            return assignment, True

        if not (
            assignment.action == "created" and assignment.status is TemplateAssignmentStatus.SUCCESS
        ):
            await self._session.rollback()
            raise TemplateAssignmentActionConflictError

        busy_workspace_id = await self._session.scalar(
            select(Workspace.id)
            .where(
                Workspace.instance_id == instance_id,
                Workspace.template_id == template_id,
                Workspace.status.in_(
                    [
                        WorkspaceStatus.PENDING,
                        WorkspaceStatus.RUNNING,
                        WorkspaceStatus.ERROR,
                    ]
                ),
            )
            .limit(1)
        )
        if busy_workspace_id is not None:
            await self._session.rollback()
            raise TemplateAssignmentWorkspacesBusyError

        job = add_job_execution(
            self._session,
            name="template_assignment.delete",
            task_name=TEMPLATE_ASSIGNMENT_DELETE_STEP_01_TASK,
            resource_type="template_assignment",
            resource_id=assignment.id,
            step=TEMPLATE_ASSIGNMENT_DELETE_STEP_01,
        )
        assignment.action = "deleting"
        assignment.status = TemplateAssignmentStatus.PENDING
        assignment.job_id = job.id
        assignment.step = TEMPLATE_ASSIGNMENT_DELETE_STEP_01
        self._session.info["enqueue_job_id"] = job.id
        await self._session.commit()
        return await self._stored_assignment(assignment.id), True

    async def _lock_instance(self, instance_id: UUID) -> Instance:
        """Lock and return the parent instance without applying mutation preconditions."""

        instance = await self._session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if instance is None:
            await self._session.rollback()
            raise TemplateAssignmentInstanceNotFoundError
        return instance

    async def _lock_template(self, template_id: UUID) -> Template:
        """Lock and return the referenced template."""

        template = await self._session.scalar(
            select(Template).where(Template.id == template_id).with_for_update()
        )
        if template is None:
            await self._session.rollback()
            raise TemplateAssignmentTemplateNotFoundError
        return template

    async def _lock_assignment(
        self,
        instance_id: UUID,
        template_id: UUID,
    ) -> TemplateAssignment | None:
        """Lock one assignment and eagerly load its public response relationships."""

        return await self._session.scalar(
            select(TemplateAssignment)
            .where(
                TemplateAssignment.instance_id == instance_id,
                TemplateAssignment.template_id == template_id,
            )
            .options(
                selectinload(TemplateAssignment.template),
                selectinload(TemplateAssignment.deployment),
            )
            .with_for_update()
        )

    async def _require_mutation_available(
        self,
        instance: Instance,
        template: Template,
    ) -> None:
        """Require a ready instance and no retryable template-wide synchronization."""

        if not (
            instance.state is InstanceState.STARTED and instance.status is InstanceStatus.SUCCESS
        ):
            await self._session.rollback()
            raise TemplateAssignmentInstanceUnavailableError
        if template.sync_status in {
            TemplateSyncStatus.PENDING,
            TemplateSyncStatus.RUNNING,
            TemplateSyncStatus.ERROR,
        }:
            await self._session.rollback()
            raise TemplateAssignmentSyncInProgressError

    async def _require_consistent_job(
        self,
        assignment: TemplateAssignment,
        *,
        job_name: str,
        task_name: str,
        step: str,
    ) -> JobExecution:
        """Require one retryable assignment to own the matching durable job."""

        job = (
            await self._session.get(JobExecution, assignment.job_id)
            if assignment.job_id is not None
            else None
        )
        consistent = (
            job is not None
            and job.name == job_name
            and job.task_name == task_name
            and job.resource_type == "template_assignment"
            and job.resource_id == assignment.id
            and job.step == step
            and assignment.step == step
            and job.status.value == assignment.status.value
        )
        if not consistent:
            await self._session.rollback()
            raise TemplateAssignmentJobConflictError
        return job

    async def _stored_assignment(self, assignment_id: UUID) -> TemplateAssignment:
        """Reload one committed assignment with relationships required by API schemas."""

        assignment = await self._session.scalar(
            select(TemplateAssignment)
            .where(TemplateAssignment.id == assignment_id)
            .options(
                selectinload(TemplateAssignment.template),
                selectinload(TemplateAssignment.deployment),
            )
        )
        if assignment is None:  # pragma: no cover - protected by the successful commit
            raise TemplateAssignmentActionConflictError
        return assignment
