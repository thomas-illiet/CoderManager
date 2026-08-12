"""API health endpoint tests."""

from httpx import AsyncClient


async def test_health_endpoint_is_exposed_outside_versioned_api(client: AsyncClient) -> None:
    """Expose liveness at the root and remove the former versioned route."""

    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert (await client.get("/api/v1/health")).status_code == 404
