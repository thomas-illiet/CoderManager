"""Template parameter CRUD endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.openapi.models import Example
from sqlalchemy.ext.asyncio import AsyncSession

from coder_manager.config import Settings, get_settings
from coder_manager.crypto import (
    CryptoConfigurationError,
    TemplateParameterCipher,
    TemplateParameterDecryptionError,
)
from coder_manager.database import get_session
from coder_manager.models import (
    TemplateParameter,
    TemplateParameterScope,
    TemplateParameterType,
)
from coder_manager.repositories import (
    TemplateParameterAlreadyExistsError,
    TemplateParameterImmutableFieldError,
    TemplateParameterNotFoundError,
    TemplateParameterRepository,
    TemplateParameterSyncInProgressError,
    TemplateParameterTemplateNotFoundError,
)
from coder_manager.schemas import (
    TemplateParameterCreate,
    TemplateParameterPage,
    TemplateParameterRead,
    TemplateParameterUpdate,
)

router = APIRouter(
    prefix="/templates/{template_id}/parameters",
    tags=["template parameters"],
)
SessionDependency = Annotated[AsyncSession, Depends(get_session)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]
NO_STORE_HEADERS = {"Cache-Control": "no-store"}
PARAMETER_CREATE_EXAMPLES: dict[str, Example] = {
    "user": {
        "summary": "Workspace-provided parameter",
        "value": {
            "type": "user",
            "name": "project_name",
            "display_name": "Project name",
            "description": "Name used by the workspace",
            "required": True,
            "mutable": False,
            "default_value": None,
        },
    },
    "system_global": {
        "summary": "Global encrypted system parameter",
        "value": {
            "type": "system",
            "name": "registry_token",
            "display_name": "Registry token",
            "description": "",
            "scope": "global",
            "value": "write-only-secret",
        },
    },
    "system_environment": {
        "summary": "Environment-specific encrypted system parameter",
        "value": {
            "type": "system",
            "name": "registry_url",
            "display_name": "Registry URL",
            "description": "",
            "scope": "environment",
            "values": {
                "development": "registry.dev.example.com",
                "staging": "registry.stg.example.com",
                "production": "registry.example.com",
            },
        },
    },
}


def parameter_cipher(settings: Settings) -> TemplateParameterCipher:
    """Build the system parameter cipher or return a redacted configuration error."""

    try:
        return TemplateParameterCipher(settings.crypto_key)
    except CryptoConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Template parameter encryption is not configured",
            headers=NO_STORE_HEADERS,
        ) from error


def parameter_read(parameter: TemplateParameter) -> TemplateParameterRead:
    """Build one representation without exposing encrypted values."""

    value_configured: bool | None = None
    values_configured: dict[str, bool] | None = None
    if parameter.type is TemplateParameterType.SYSTEM:
        targets = {value.target.value for value in parameter.system_values}
        if parameter.scope is TemplateParameterScope.GLOBAL:
            value_configured = "global" in targets
        else:
            values_configured = {
                environment: environment in targets
                for environment in ("development", "staging", "production")
            }
    return TemplateParameterRead(
        id=parameter.id,
        template_id=parameter.template_id,
        type=parameter.type,
        name=parameter.name,
        display_name=parameter.display_name,
        description=parameter.description,
        required=parameter.required,
        mutable=parameter.mutable,
        default_value=parameter.default_value,
        scope=parameter.scope,
        value_configured=value_configured,
        values_configured=values_configured,
        created_at=parameter.created_at,
        updated_at=parameter.updated_at,
    )


@router.get("", summary="List a template's parameters")
async def list_template_parameters(
    template_id: UUID,
    session: SessionDependency,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TemplateParameterPage:
    """Return one deterministic redacted parameter page."""

    try:
        parameters, total = await TemplateParameterRepository(session).list(
            template_id,
            page=page,
            page_size=page_size,
        )
    except TemplateParameterTemplateNotFoundError as error:
        raise _template_not_found() from error
    pages = (total + page_size - 1) // page_size
    return TemplateParameterPage(
        items=[parameter_read(parameter) for parameter in parameters],
        page=page,
        page_size=page_size,
        total=total,
        pages=pages,
    )


@router.get("/{parameter_id}", summary="Get a template parameter")
async def get_template_parameter(
    template_id: UUID,
    parameter_id: UUID,
    session: SessionDependency,
) -> TemplateParameterRead:
    """Return one redacted parameter constrained to its parent template."""

    parameter = await TemplateParameterRepository(session).get(template_id, parameter_id)
    if parameter is None:
        raise _parameter_not_found()
    return parameter_read(parameter)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a template parameter",
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": PARAMETER_CREATE_EXAMPLES,
                }
            }
        }
    },
)
async def create_template_parameter(
    template_id: UUID,
    payload: TemplateParameterCreate,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
) -> TemplateParameterRead:
    """Create one user definition or encrypted system parameter."""

    cipher = parameter_cipher(settings) if payload.type is TemplateParameterType.SYSTEM else None
    try:
        parameter = await TemplateParameterRepository(session).create(
            template_id,
            payload,
            cipher,
        )
    except TemplateParameterTemplateNotFoundError as error:
        raise _template_not_found() from error
    except TemplateParameterAlreadyExistsError as error:
        raise _parameter_conflict() from error
    except TemplateParameterSyncInProgressError as error:
        raise _sync_conflict() from error
    if payload.type is TemplateParameterType.SYSTEM:
        response.headers.update(NO_STORE_HEADERS)
    return parameter_read(parameter)


@router.put("/{parameter_id}", summary="Replace a template parameter")
async def update_template_parameter(  # noqa: PLR0913
    template_id: UUID,
    parameter_id: UUID,
    payload: TemplateParameterUpdate,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
) -> TemplateParameterRead:
    """Replace mutable metadata and optionally rotate encrypted values."""

    cipher = parameter_cipher(settings) if payload.type is TemplateParameterType.SYSTEM else None
    try:
        parameter = await TemplateParameterRepository(session).update(
            template_id,
            parameter_id,
            payload,
            cipher,
        )
    except TemplateParameterTemplateNotFoundError as error:
        raise _template_not_found() from error
    except TemplateParameterNotFoundError as error:
        raise _parameter_not_found() from error
    except TemplateParameterImmutableFieldError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Template parameter type and scope are immutable",
        ) from error
    except TemplateParameterSyncInProgressError as error:
        raise _sync_conflict() from error
    except TemplateParameterDecryptionError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Template parameter value cannot be decrypted",
            headers=NO_STORE_HEADERS,
        ) from error
    if payload.type is TemplateParameterType.SYSTEM:
        response.headers.update(NO_STORE_HEADERS)
    return parameter_read(parameter)


@router.delete(
    "/{parameter_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a template parameter",
)
async def delete_template_parameter(
    template_id: UUID,
    parameter_id: UUID,
    session: SessionDependency,
) -> Response:
    """Delete one parameter while preserving workspace snapshots."""

    try:
        await TemplateParameterRepository(session).delete(template_id, parameter_id)
    except TemplateParameterTemplateNotFoundError as error:
        raise _template_not_found() from error
    except TemplateParameterNotFoundError as error:
        raise _parameter_not_found() from error
    except TemplateParameterSyncInProgressError as error:
        raise _sync_conflict() from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _template_not_found() -> HTTPException:
    """Build the standard unknown-template response."""

    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")


def _parameter_not_found() -> HTTPException:
    """Build the standard unknown-parameter response."""

    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Template parameter not found",
    )


def _parameter_conflict() -> HTTPException:
    """Build the standard duplicate-name response."""

    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="A parameter with this name already exists in the template",
    )


def _sync_conflict() -> HTTPException:
    """Build the standard active-sync response."""

    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Template synchronization is already in progress",
    )
