"""Coder template CRUD endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from coder_manager.database import get_session
from coder_manager.repositories import (
    TemplateAlreadyExistsError,
    TemplateHasWorkspacesError,
    TemplateNotFoundError,
    TemplateRepository,
    TemplateWorkspaceCompatibilityError,
)
from coder_manager.schemas import (
    TemplateCreate,
    TemplateListQuery,
    TemplatePage,
    TemplateRead,
    TemplateUpdate,
)

router = APIRouter(prefix="/templates", tags=["templates"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]


@router.get("", summary="List Coder templates")
async def list_templates(
    session: SessionDependency,
    query: Annotated[TemplateListQuery, Query()],
) -> TemplatePage:
    """Return a filtered page ordered deterministically by template name."""

    templates, total = await TemplateRepository(session).list(
        page=query.page,
        page_size=query.page_size,
        scope=query.scope,
        application=query.application,
        name=query.name,
    )
    pages = (total + query.page_size - 1) // query.page_size
    return TemplatePage(
        items=[TemplateRead.model_validate(template) for template in templates],
        page=query.page,
        page_size=query.page_size,
        total=total,
        pages=pages,
    )


@router.get("/{template_id}/modules", summary="List a template's modules")
async def list_template_modules(template_id: UUID, session: SessionDependency) -> list[str]:
    """Return the ordered module names directly as a JSON string array."""

    template = await TemplateRepository(session).get(template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")
    return list(template.modules)


@router.get("/{template_id}", summary="Get a Coder template")
async def get_template(template_id: UUID, session: SessionDependency) -> TemplateRead:
    """Return one template or a 404 response."""

    template = await TemplateRepository(session).get(template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")
    return TemplateRead.model_validate(template)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a Coder template",
)
async def create_template(payload: TemplateCreate, session: SessionDependency) -> TemplateRead:
    """Create a template while enforcing its scope and name uniqueness."""

    try:
        template = await TemplateRepository(session).create(payload)
    except TemplateAlreadyExistsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A template with this name already exists in this scope",
        ) from error
    return TemplateRead.model_validate(template)


@router.put("/{template_id}", summary="Replace a Coder template's mutable fields")
async def update_template(
    template_id: UUID,
    payload: TemplateUpdate,
    session: SessionDependency,
) -> TemplateRead:
    """Replace mutable fields without allowing the template scope to change."""

    try:
        template = await TemplateRepository(session).update(template_id, payload)
    except TemplateNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Template not found",
        ) from error
    except TemplateAlreadyExistsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A template with this name already exists in this scope",
        ) from error
    except TemplateWorkspaceCompatibilityError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Template changes would invalidate existing workspaces",
        ) from error
    return TemplateRead.model_validate(template)


@router.delete(
    "/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a Coder template",
)
async def delete_template(template_id: UUID, session: SessionDependency) -> Response:
    """Delete one template or return a 404 response."""

    try:
        await TemplateRepository(session).delete(template_id)
    except TemplateNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Template not found",
        ) from error
    except TemplateHasWorkspacesError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Template still has workspaces",
        ) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
