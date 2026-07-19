"""Service health endpoint."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health", summary="Check that the API process is running")
async def health() -> dict[str, str]:
    """Return a lightweight liveness response."""

    return {"status": "ok"}
