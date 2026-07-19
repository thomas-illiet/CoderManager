"""Application API behavior tests."""

from datetime import datetime
from uuid import uuid4

from httpx import AsyncClient

from coder_manager.config import Settings, get_settings
from coder_manager.main import app


async def create_application(
    client: AsyncClient,
    *,
    external_id: str = "business-app-1",
    name: str = "Development",
) -> dict[str, object]:
    """Create a application used by the test scenario."""

    response = await client.post(
        "/api/v1/applications",
        json={"external_id": external_id, "name": name},
    )
    assert response.status_code == 201
    return response.json()


async def test_application_crud(client: AsyncClient) -> None:
    """Verify the application crud scenario."""

    created = await create_application(client)
    assert created["whitelist"] is False
    assert datetime.fromisoformat(created["created_at"])

    fetched = await client.get(f"/api/v1/applications/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created

    deleted = await client.delete(f"/api/v1/applications/{created['id']}")
    assert deleted.status_code == 204
    assert deleted.content == b""

    missing = await client.get(f"/api/v1/applications/{created['id']}")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Application not found"}


async def test_list_applications_is_paginated(client: AsyncClient) -> None:
    """Verify the list applications is paginated scenario."""

    await create_application(client, external_id="business-app-b", name="Beta")
    await create_application(client, external_id="business-app-a", name="Alpha")

    first_page = await client.get("/api/v1/applications", params={"page": 1, "page_size": 1})
    assert first_page.status_code == 200
    assert first_page.json() == {
        "items": [first_page.json()["items"][0]],
        "page": 1,
        "page_size": 1,
        "total": 2,
        "pages": 2,
    }
    assert first_page.json()["items"][0]["name"] == "Alpha"

    second_page = await client.get("/api/v1/applications", params={"page": 2, "page_size": 1})
    assert second_page.json()["items"][0]["name"] == "Beta"


async def test_application_whitelist_is_idempotent(client: AsyncClient) -> None:
    """Verify the application whitelist is idempotent scenario."""

    application = await create_application(client)
    path = f"/api/v1/applications/{application['id']}/whitelist"

    for _ in range(2):
        enabled = await client.post(path)
        assert enabled.status_code == 204
        assert enabled.content == b""

    fetched = await client.get(f"/api/v1/applications/{application['id']}")
    assert fetched.json()["whitelist"] is True

    for _ in range(2):
        disabled = await client.delete(path)
        assert disabled.status_code == 204
        assert disabled.content == b""

    fetched = await client.get(f"/api/v1/applications/{application['id']}")
    assert fetched.json()["whitelist"] is False


async def test_list_applications_filters_by_whitelist_and_literal_name(
    client: AsyncClient,
) -> None:
    """Verify the list applications filters by whitelist and literal name scenario."""

    portal = await create_application(client, external_id="portal", name="Customer Portal")
    await create_application(client, external_id="other", name="Other Application")
    percentage = await create_application(client, external_id="percentage", name="100% Portal")
    await client.post(f"/api/v1/applications/{portal['id']}/whitelist")
    await client.post(f"/api/v1/applications/{percentage['id']}/whitelist")

    filtered = await client.get(
        "/api/v1/applications",
        params={"whitelist": "true", "name": "PORTAL"},
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 2
    assert filtered.json()["pages"] == 1
    assert all(item["whitelist"] is True for item in filtered.json()["items"])

    non_whitelisted = await client.get(
        "/api/v1/applications",
        params={"whitelist": "false"},
    )
    assert non_whitelisted.json()["total"] == 1
    assert non_whitelisted.json()["items"][0]["name"] == "Other Application"

    literal_wildcard = await client.get("/api/v1/applications", params={"name": "%"})
    assert literal_wildcard.json()["total"] == 1
    assert literal_wildcard.json()["items"][0]["id"] == percentage["id"]


async def test_global_whitelist_is_live_and_disables_individual_changes(
    client: AsyncClient,
) -> None:
    """Verify the global whitelist is live and disables individual changes scenario."""

    application = await create_application(client)
    whitelist_path = f"/api/v1/applications/{application['id']}/whitelist"
    app.dependency_overrides[get_settings] = lambda: Settings(global_whitelist=True)

    fetched = await client.get(f"/api/v1/applications/{application['id']}")
    listed = await client.get("/api/v1/applications", params={"whitelist": "true"})
    filtered_out = await client.get("/api/v1/applications", params={"whitelist": "false"})
    created = await create_application(client, external_id="global-app", name="Global App")

    assert fetched.json()["whitelist"] is True
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["whitelist"] is True
    assert filtered_out.json()["total"] == 0
    assert filtered_out.json()["items"] == []
    assert created["whitelist"] is True

    for method in (client.post, client.delete):
        unavailable = await method(whitelist_path)
        assert unavailable.status_code == 409
        assert unavailable.json() == {"detail": "Application whitelist management is unavailable"}

    app.dependency_overrides.pop(get_settings)
    persisted = await client.get(f"/api/v1/applications/{application['id']}")
    assert persisted.json()["whitelist"] is False


async def test_duplicate_external_id_returns_conflict(client: AsyncClient) -> None:
    """Verify the duplicate external id returns conflict scenario."""

    await create_application(client)
    duplicate = await client.post(
        "/api/v1/applications",
        json={"external_id": "business-app-1", "name": "Duplicate"},
    )

    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "An application with this external_id already exists"}


async def test_invalid_payload_and_pagination_are_rejected(client: AsyncClient) -> None:
    """Verify the invalid payload and pagination are rejected scenario."""

    invalid_payload = await client.post(
        "/api/v1/applications",
        json={"external_id": "   ", "name": ""},
    )
    invalid_page = await client.get("/api/v1/applications", params={"page": 0, "page_size": 101})

    assert invalid_payload.status_code == 422
    assert invalid_page.status_code == 422


async def test_missing_delete_returns_not_found(client: AsyncClient) -> None:
    """Verify the missing delete returns not found scenario."""

    application_id = uuid4()
    response = await client.delete(f"/api/v1/applications/{application_id}")
    whitelist = await client.post(f"/api/v1/applications/{application_id}/whitelist")
    unwhitelist = await client.delete(f"/api/v1/applications/{application_id}/whitelist")

    assert response.status_code == 404
    assert whitelist.status_code == 404
    assert unwhitelist.status_code == 404


async def test_health(client: AsyncClient) -> None:
    """Verify the health scenario."""

    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
