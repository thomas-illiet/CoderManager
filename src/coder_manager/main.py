"""FastAPI application entrypoint."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2AuthorizationCodeBearer

from coder_manager.api.router import api_router
from coder_manager.auth import OidcAuthenticator, OidcConfig, build_oidc_dependency
from coder_manager.config import Settings, get_settings
from coder_manager.metrics import ApiMetrics, ApiMetricsMiddleware, start_metrics_server


def redacted_validation_errors(error: RequestValidationError) -> list[dict[str, Any]]:
    """Remove credential inputs from validation details before returning them."""

    errors: list[dict[str, Any]] = []
    for detail in error.errors():
        safe_detail = dict(detail)
        if any(
            credential in str(part).lower()
            for part in detail.get("loc", ())
            for credential in ("password", "token")
        ):
            safe_detail["input"] = "[REDACTED]"
            safe_detail.pop("ctx", None)
        errors.append(safe_detail)
    return errors


async def validation_exception_handler(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    """Return standard validation details with credential inputs removed."""

    if not isinstance(error, RequestValidationError):  # pragma: no cover - registration invariant
        raise error
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": jsonable_encoder(redacted_validation_errors(error))},
    )


def create_app(
    *,
    settings: Settings | None = None,
    oidc_authenticator_factory: Callable[[OidcConfig], OidcAuthenticator] = OidcAuthenticator,
) -> FastAPI:
    """Build the HTTP application."""

    settings = settings or get_settings()
    metrics = ApiMetrics()
    oidc_config = OidcConfig.from_settings(settings)
    oidc_authenticator = (
        oidc_authenticator_factory(oidc_config) if oidc_config is not None else None
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """Initialize OIDC and expose metrics for the lifetime of the API process."""

        server = None
        try:
            if oidc_authenticator is not None:
                await oidc_authenticator.initialize()
            server = start_metrics_server(
                settings.metrics_host,
                settings.metrics_port,
                metrics.registry,
            )
            application.state.metrics_server = server
            yield
        finally:
            if server is not None:
                server.stop()

    swagger_ui_init_oauth = None
    if oidc_config is not None:
        swagger_ui_init_oauth = {
            "clientId": oidc_config.client_id,
            "scopes": " ".join(oidc_config.scopes),
            "usePkceWithAuthorizationCodeGrant": True,
        }
    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        swagger_ui_init_oauth=swagger_ui_init_oauth,
    )
    application.state.api_metrics = metrics
    application.state.oidc_authenticator = oidc_authenticator
    application.add_middleware(ApiMetricsMiddleware, metrics=metrics)
    application.add_exception_handler(RequestValidationError, validation_exception_handler)
    dependencies = None
    if oidc_config is not None and oidc_authenticator is not None:
        oauth2_scheme = OAuth2AuthorizationCodeBearer(
            authorizationUrl=oidc_config.authorization_url,
            tokenUrl=oidc_config.token_url,
            scopes={scope: scope for scope in oidc_config.scopes},
            auto_error=False,
        )
        dependencies = [Depends(build_oidc_dependency(oidc_authenticator, oauth2_scheme))]
    application.include_router(api_router, prefix="/api/v1", dependencies=dependencies)
    return application


app = create_app()


def run() -> None:
    """Run the development HTTP server."""

    uvicorn.run("coder_manager.main:app", host="0.0.0.0", port=8000, reload=True)  # noqa: S104
