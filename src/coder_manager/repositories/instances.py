"""Persistence operations for Coder instances."""

import re
import unicodedata
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from coder_manager.models import (
    Application,
    Database,
    DatabaseAllocation,
    Instance,
    InstanceEnvironment,
    InstanceStatus,
    Member,
    MemberStatus,
)
from coder_manager.repositories.job_executions import add_job_execution
from coder_manager.schemas import InstanceCreate
from coder_manager.tasks.common.registry import (
    INSTANCE_CREATE_STEP_01,
    INSTANCE_CREATE_STEP_01_TASK,
    INSTANCE_DELETE_STEP_01,
    INSTANCE_DELETE_STEP_01_TASK,
    INSTANCE_UPDATE_STEP_01,
    INSTANCE_UPDATE_STEP_01_TASK,
)

MAX_ACTION_LENGTH = 255
MAX_DNS_LABEL_LENGTH = 63
ENVIRONMENT_DNS_LABELS = {
    InstanceEnvironment.DEVELOPMENT: "dev",
    InstanceEnvironment.STAGING: "staging",
    InstanceEnvironment.PRODUCTION: "cib",
}


class InstanceAlreadyExistsError(Exception):
    """Raised when an instance conflicts with an existing placement or URL."""


class InstanceApplicationNotFoundError(Exception):
    """Raised when an instance references an unknown application."""


class InstanceApplicationNotWhitelistedError(Exception):
    """Raised when an instance references an application that is not allowed."""


class InvalidApplicationSlugError(Exception):
    """Raised when an application name cannot produce a valid DNS label."""


class InstanceNotFoundError(Exception):
    """Raised when an instance cannot be found."""


class InstanceActionConflictError(Exception):
    """Raised when an action transition is incompatible with the current state."""


class InstanceDatabaseUnavailableError(Exception):
    """Raised when no database in the requested region has a free slot."""


class InvalidInstanceActionError(Exception):
    """Raised when an internal action name is empty or too long."""


def application_slug(name: str) -> str:
    """Convert an application name into a lowercase ASCII DNS label."""

    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
    if not slug or len(slug) > MAX_DNS_LABEL_LENGTH:
        raise InvalidApplicationSlugError
    return slug


def instance_url(application_name: str, payload: InstanceCreate) -> str:
    """Build the immutable public URL for a new instance."""

    slug = application_slug(application_name)
    environment = ENVIRONMENT_DNS_LABELS[payload.environment]
    return f"https://{slug}.{payload.region.value}.code-studio.{environment}.echonet"


class InstanceRepository:
    """Store and transition Coder instances using an async SQLAlchemy session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the database session used by repository operations."""

        self._session = session

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        application_id: UUID | None = None,
        database_id: UUID | None = None,
    ) -> tuple[list[Instance], int]:
        """Return one deterministic page and the matching instance count."""

        count_statement = select(func.count()).select_from(Instance)
        list_statement = select(Instance).options(selectinload(Instance.database_allocation))
        if application_id is not None:
            count_statement = count_statement.where(Instance.application_id == application_id)
            list_statement = list_statement.where(Instance.application_id == application_id)
        if database_id is not None:
            count_statement = count_statement.join(DatabaseAllocation).where(
                DatabaseAllocation.database_id == database_id
            )
            list_statement = list_statement.join(DatabaseAllocation).where(
                DatabaseAllocation.database_id == database_id
            )

        total = await self._session.scalar(count_statement)
        result = await self._session.scalars(
            list_statement.order_by(
                Instance.application_id,
                Instance.region,
                Instance.environment,
                Instance.id,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(result), total or 0

    async def get(self, instance_id: UUID) -> Instance | None:
        """Find an instance by its identifier."""

        return await self._session.scalar(
            select(Instance)
            .where(Instance.id == instance_id)
            .options(selectinload(Instance.database_allocation))
        )

    async def create(
        self,
        payload: InstanceCreate,
        *,
        global_whitelist: bool = False,
    ) -> Instance:
        """Create an instance and reserve capacity on the least utilized regional database."""

        # Validate the application boundary before locking regional capacity.
        application = await self._session.get(Application, payload.application_id)
        if application is None:
            raise InstanceApplicationNotFoundError
        if not global_whitelist and not application.whitelist:
            raise InstanceApplicationNotWhitelistedError

        # Lock every regional candidate so concurrent requests cannot overbook a database.
        databases = list(
            await self._session.scalars(
                select(Database)
                .where(Database.region == payload.region)
                .order_by(func.lower(Database.name), Database.name, Database.id)
                .with_for_update()
            )
        )
        if not databases:
            await self._session.rollback()
            raise InstanceDatabaseUnavailableError

        # Include empty databases, which are absent from the grouped allocation query.
        allocation_counts = {database.id: 0 for database in databases}
        count_rows = await self._session.execute(
            select(DatabaseAllocation.database_id, func.count(DatabaseAllocation.id))
            .where(DatabaseAllocation.database_id.in_(allocation_counts))
            .group_by(DatabaseAllocation.database_id)
        )
        allocation_counts.update(dict(count_rows.tuples().all()))
        available_databases = [
            database
            for database in databases
            if allocation_counts[database.id] < database.instance_max
        ]
        if not available_databases:
            await self._session.rollback()
            raise InstanceDatabaseUnavailableError

        # Prefer the lowest utilization ratio, then use stable tie breakers.
        database = min(
            available_databases,
            key=lambda candidate: (
                allocation_counts[candidate.id] / candidate.instance_max,
                candidate.name.casefold(),
                str(candidate.id),
            ),
        )
        # Persist the instance and its reserved database slot atomically.
        instance_id = uuid4()
        instance = Instance(
            id=instance_id,
            application_id=application.id,
            region=payload.region,
            environment=payload.environment,
            action="creating",
            status=InstanceStatus.PENDING,
            instance_url=instance_url(application.name, payload),
            step=INSTANCE_CREATE_STEP_01,
        )
        job = add_job_execution(
            self._session,
            name="instance.create",
            task_name=INSTANCE_CREATE_STEP_01_TASK,
            resource_type="instance",
            resource_id=instance_id,
            step=INSTANCE_CREATE_STEP_01,
        )
        instance.job_id = job.id
        allocation = DatabaseAllocation(
            database_id=database.id,
            instance_id=instance_id,
            schema_name=f"coder_{instance_id.hex}",
        )
        instance.database_allocation = allocation
        self._session.add_all((instance, allocation))
        try:
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise InstanceAlreadyExistsError from error
        stored_instance = await self._session.scalar(
            select(Instance)
            .where(Instance.id == instance.id)
            .options(selectinload(Instance.database_allocation))
        )
        if stored_instance is None:  # pragma: no cover - protected by the successful commit
            raise InstanceNotFoundError
        return stored_instance

    async def request_deletion(self, instance_id: UUID) -> Instance:
        """Atomically request deletion of a successfully reconciled instance."""

        # Lock the instance so only one lifecycle transition can win.
        instance = await self._session.scalar(
            select(Instance)
            .where(Instance.id == instance_id)
            .options(selectinload(Instance.database_allocation))
            .with_for_update()
        )
        if instance is None:
            await self._session.rollback()
            raise InstanceNotFoundError
        if (
            instance.action not in {"creating", "updating"}
            or instance.status is not InstanceStatus.SUCCESS
        ):
            await self._session.rollback()
            raise InstanceActionConflictError

        # The worker performs destructive cleanup asynchronously from this request.
        instance.action = "deleting"
        instance.status = InstanceStatus.PENDING
        job = add_job_execution(
            self._session,
            name="instance.delete",
            task_name=INSTANCE_DELETE_STEP_01_TASK,
            resource_type="instance",
            resource_id=instance.id,
            step=INSTANCE_DELETE_STEP_01,
        )
        instance.job_id = job.id
        instance.step = INSTANCE_DELETE_STEP_01
        await self._session.commit()
        stored_instance = await self._session.scalar(
            select(Instance)
            .where(Instance.id == instance_id)
            .options(selectinload(Instance.database_allocation))
        )
        if stored_instance is None:  # pragma: no cover - protected by the successful commit
            raise InstanceNotFoundError
        return stored_instance

    async def request_sync(self, instance_id: UUID) -> Instance:
        """Request one reconciliation without competing with another lifecycle job."""

        # Serialize manual sync requests with all other instance transitions.
        instance = await self._session.scalar(
            select(Instance)
            .where(Instance.id == instance_id)
            .options(selectinload(Instance.database_allocation))
            .with_for_update()
        )
        if instance is None:
            await self._session.rollback()
            raise InstanceNotFoundError
        if instance.action == "deleting" or instance.status in {
            InstanceStatus.PENDING,
            InstanceStatus.RUNNING,
        }:
            await self._session.rollback()
            raise InstanceActionConflictError

        # Retry failed member mutations in the same reconciliation pass.
        failed_members = await self._session.scalars(
            select(Member)
            .where(
                Member.instance_id == instance_id,
                Member.status == MemberStatus.ERROR,
            )
            .with_for_update()
        )
        for member in failed_members:
            member.status = MemberStatus.PENDING
        instance.action = "updating"
        instance.status = InstanceStatus.PENDING
        job = add_job_execution(
            self._session,
            name="instance.update",
            task_name=INSTANCE_UPDATE_STEP_01_TASK,
            resource_type="instance",
            resource_id=instance.id,
            step=INSTANCE_UPDATE_STEP_01,
        )
        instance.job_id = job.id
        instance.step = INSTANCE_UPDATE_STEP_01
        await self._session.commit()
        stored_instance = await self._session.scalar(
            select(Instance)
            .where(Instance.id == instance_id)
            .options(selectinload(Instance.database_allocation))
        )
        if stored_instance is None:  # pragma: no cover - protected by the successful commit
            raise InstanceNotFoundError
        return stored_instance

    async def update_action(
        self,
        instance_id: UUID,
        *,
        expected_action: str,
        action: str,
        status: InstanceStatus,
    ) -> Instance:
        """Update action state while rejecting results from a stale action."""

        normalized_action = action.strip()
        if not normalized_action or len(normalized_action) > MAX_ACTION_LENGTH:
            raise InvalidInstanceActionError

        instance = await self._session.scalar(
            select(Instance).where(Instance.id == instance_id).with_for_update()
        )
        if instance is None:
            await self._session.rollback()
            raise InstanceNotFoundError
        if instance.action != expected_action:
            await self._session.rollback()
            raise InstanceActionConflictError

        instance.action = normalized_action
        instance.status = status
        await self._session.commit()
        await self._session.refresh(instance)
        return instance
