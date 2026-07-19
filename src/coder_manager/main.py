"""FastAPI application entrypoint."""

from typing import Any

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from coder_manager.api.router import api_router
from coder_manager.config import get_settings


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


def create_app() -> FastAPI:
    """Build the HTTP application."""

    settings = get_settings()
    application = FastAPI(title=settings.app_name, version="0.1.0")
    application.add_exception_handler(RequestValidationError, validation_exception_handler)
    application.include_router(api_router, prefix="/api/v1")
    return application


app = create_app()


def run() -> None:
    """Run the development HTTP server."""

    uvicorn.run("coder_manager.main:app", host="0.0.0.0", port=8000, reload=True)  # noqa: S104
