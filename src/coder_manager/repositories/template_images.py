"""Persistence operations for Docker images allowed by templates."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coder_manager.models import Template, TemplateImage, Workspace
from coder_manager.schemas import TemplateImageCreate


class TemplateImageAlreadyExistsError(Exception):
    """Raised when an identical image is already allowed by a template."""


class TemplateImageNotFoundError(Exception):
    """Raised when a template image does not exist in the requested template."""


class TemplateImageTemplateNotFoundError(Exception):
    """Raised when an image operation references an unknown template."""


class TemplateImageInUseError(Exception):
    """Raised when an image is referenced by a workspace."""


class TemplateImageRepository:
    """Store immutable Docker images allowed by one template."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the database session used by repository operations."""

        self._session = session

    async def list(
        self, template_id: UUID, *, page: int, page_size: int
    ) -> tuple[list[TemplateImage], int]:
        """Return one deterministic image page after validating its template."""

        if await self._session.get(Template, template_id) is None:
            raise TemplateImageTemplateNotFoundError
        image_filter = TemplateImage.template_id == template_id
        total = await self._session.scalar(
            select(func.count()).select_from(TemplateImage).where(image_filter)
        )
        result = await self._session.scalars(
            select(TemplateImage)
            .where(image_filter)
            .order_by(
                TemplateImage.registry_name,
                TemplateImage.name,
                TemplateImage.version,
                TemplateImage.id,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(result), total or 0

    async def get(self, template_id: UUID, image_id: UUID) -> TemplateImage | None:
        """Find one image only within its parent template."""

        return await self._session.scalar(
            select(TemplateImage).where(
                TemplateImage.id == image_id,
                TemplateImage.template_id == template_id,
            )
        )

    async def create(self, template_id: UUID, payload: TemplateImageCreate) -> TemplateImage:
        """Allow a normalized immutable Docker image on a template."""

        template = await self._session.scalar(
            select(Template).where(Template.id == template_id).with_for_update()
        )
        if template is None:
            await self._session.rollback()
            raise TemplateImageTemplateNotFoundError
        image = TemplateImage(
            template_id=template.id,
            registry_name=payload.registry,
            name=payload.name,
            version=payload.version,
        )
        self._session.add(image)
        try:
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise TemplateImageAlreadyExistsError from error
        await self._session.refresh(image)
        return image

    async def delete(self, template_id: UUID, image_id: UUID) -> None:
        """Delete an unused image constrained to its parent template."""

        if await self._session.get(Template, template_id) is None:
            raise TemplateImageTemplateNotFoundError
        image = await self._session.scalar(
            select(TemplateImage)
            .where(
                TemplateImage.id == image_id,
                TemplateImage.template_id == template_id,
            )
            .with_for_update()
        )
        if image is None:
            await self._session.rollback()
            raise TemplateImageNotFoundError
        workspace_id = await self._session.scalar(
            select(Workspace.id).where(Workspace.image_id == image_id).limit(1)
        )
        if workspace_id is not None:
            await self._session.rollback()
            raise TemplateImageInUseError
        await self._session.delete(image)
        await self._session.commit()
