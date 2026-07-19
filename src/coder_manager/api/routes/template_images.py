"""Template Docker image endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from coder_manager.database import get_session
from coder_manager.repositories import (
    TemplateImageAlreadyExistsError,
    TemplateImageInUseError,
    TemplateImageNotFoundError,
    TemplateImageRepository,
    TemplateImageTemplateNotFoundError,
)
from coder_manager.schemas import TemplateImageCreate, TemplateImagePage, TemplateImageRead

router = APIRouter(prefix="/templates/{template_id}/images", tags=["template images"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]


@router.get("", summary="List a template's allowed Docker images")
async def list_template_images(
    template_id: UUID,
    session: SessionDependency,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TemplateImagePage:
    """Return a deterministic page of images allowed by one template."""

    try:
        images, total = await TemplateImageRepository(session).list(
            template_id, page=page, page_size=page_size
        )
    except TemplateImageTemplateNotFoundError as error:
        raise _template_not_found() from error
    pages = (total + page_size - 1) // page_size
    return TemplateImagePage(
        items=[TemplateImageRead.model_validate(image) for image in images],
        page=page,
        page_size=page_size,
        total=total,
        pages=pages,
    )


@router.get("/{image_id}", summary="Get an allowed Docker image")
async def get_template_image(
    template_id: UUID, image_id: UUID, session: SessionDependency
) -> TemplateImageRead:
    """Return one image constrained to its parent template."""

    image = await TemplateImageRepository(session).get(template_id, image_id)
    if image is None:
        raise _image_not_found()
    return TemplateImageRead.model_validate(image)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Allow a Docker image")
async def create_template_image(
    template_id: UUID,
    payload: TemplateImageCreate,
    session: SessionDependency,
) -> TemplateImageRead:
    """Create a normalized immutable image reference."""

    try:
        image = await TemplateImageRepository(session).create(template_id, payload)
    except TemplateImageTemplateNotFoundError as error:
        raise _template_not_found() from error
    except TemplateImageAlreadyExistsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Docker image is already allowed by this template",
        ) from error
    return TemplateImageRead.model_validate(image)


@router.delete(
    "/{image_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a Docker image"
)
async def delete_template_image(
    template_id: UUID, image_id: UUID, session: SessionDependency
) -> Response:
    """Delete an unused image constrained to its parent template."""

    try:
        await TemplateImageRepository(session).delete(template_id, image_id)
    except TemplateImageTemplateNotFoundError as error:
        raise _template_not_found() from error
    except TemplateImageNotFoundError as error:
        raise _image_not_found() from error
    except TemplateImageInUseError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Docker image is still used by workspaces",
        ) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _template_not_found() -> HTTPException:
    """Build the standard response for an unknown template."""

    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")


def _image_not_found() -> HTTPException:
    """Build the standard response for an unknown Docker image."""

    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Docker image not found")
