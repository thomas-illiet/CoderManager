"""Persistence operations for applications."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coder_manager.models import Application, Instance
from coder_manager.schemas import ApplicationCreate


class ApplicationAlreadyExistsError(Exception):
    """Raised when an application uses an existing external identifier."""


class ApplicationHasInstancesError(Exception):
    """Raised when deleting an application that still owns instances."""


class ApplicationRepository:
    """Store and retrieve applications using an async SQLAlchemy session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the database session used by repository operations."""

        self._session = session

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        whitelist: bool | None = None,
        name: str | None = None,
        global_whitelist: bool = False,
    ) -> tuple[list[Application], int]:
        """Return one deterministic filtered page and its matching total."""

        count_statement = select(func.count()).select_from(Application)
        list_statement = select(Application)

        if whitelist is not None:
            if global_whitelist:
                if not whitelist:
                    return [], 0
            else:
                whitelist_condition = Application.whitelist.is_(whitelist)
                count_statement = count_statement.where(whitelist_condition)
                list_statement = list_statement.where(whitelist_condition)
        if name is not None:
            name_condition = Application.name.icontains(name, autoescape=True)
            count_statement = count_statement.where(name_condition)
            list_statement = list_statement.where(name_condition)

        total = await self._session.scalar(count_statement)
        result = await self._session.scalars(
            list_statement.order_by(Application.name, Application.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(result), total or 0

    async def get(self, application_id: UUID) -> Application | None:
        """Find an application by its internal identifier."""

        return await self._session.get(Application, application_id)

    async def create(self, payload: ApplicationCreate) -> Application:
        """Create an application and persist it immediately."""

        application = Application(external_id=payload.external_id, name=payload.name)
        self._session.add(application)
        try:
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise ApplicationAlreadyExistsError from error
        await self._session.refresh(application)
        return application

    async def set_whitelist(self, application: Application, *, enabled: bool) -> None:
        """Persist an application's individual whitelist state."""

        application.whitelist = enabled
        await self._session.commit()

    async def delete(self, application: Application) -> None:
        """Delete an application and persist the change immediately."""

        instance_id = await self._session.scalar(
            select(Instance.id).where(Instance.application_id == application.id).limit(1)
        )
        if instance_id is not None:
            raise ApplicationHasInstancesError

        await self._session.delete(application)
        try:
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise ApplicationHasInstancesError from error
