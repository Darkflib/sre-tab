"""openapi.json is served, complete, and matches the PRD endpoint table."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.main import create_app
from app.settings import Settings

# The PRD's twelve endpoints plus healthz: (path, method) operations.
EXPECTED_OPERATIONS = {
    ("/api/v1/healthz", "get"),
    ("/api/v1/auth/github/start", "get"),
    ("/api/v1/auth/github/callback", "get"),
    ("/api/v1/auth/logout", "post"),
    ("/api/v1/me", "get"),
    ("/api/v1/me/preferences", "patch"),
    ("/api/v1/me", "delete"),
    ("/api/v1/sources", "get"),
    ("/api/v1/feed", "get"),
    ("/api/v1/items/{item_id}/read-state", "put"),
    ("/api/v1/bookmarks", "get"),
    ("/api/v1/items/{item_id}/bookmark", "put"),
    ("/api/v1/items/{item_id}/bookmark", "delete"),
}


def test_openapi_served_and_complete(client: TestClient) -> None:
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]

    operations = {
        (path, method)
        for path, item in paths.items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    }
    # Exact equality: a missing stub and an accidental extra route both fail.
    assert operations == EXPECTED_OPERATIONS


def test_swagger_ui_is_off_by_default(client: TestClient) -> None:
    """Default-closed, and asserted from the default rather than from a
    fixture: the conftest settings never mention ``docs_enabled``, so a
    deployment that sets nothing gets this behaviour."""
    assert Settings.model_fields["docs_enabled"].default is False
    assert client.get("/docs").status_code == 404


def test_openapi_is_served_with_the_ui_disabled(client: TestClient) -> None:
    """Publishing the schema is a v1 requirement; the UI is not the schema."""
    assert client.get("/api/v1/openapi.json").status_code == 200


def test_swagger_ui_is_available_when_enabled(settings: Settings, engine: Engine) -> None:
    application = create_app(settings.model_copy(update={"docs_enabled": True}), engine=engine)
    with TestClient(application) as enabled:
        response = enabled.get("/docs")
    assert response.status_code == 200
    assert "swagger-ui" in response.text.lower()


def test_openapi_schemas_carry_the_contract(client: TestClient) -> None:
    spec = client.get("/api/v1/openapi.json").json()
    component_schemas = spec["components"]["schemas"]
    for name in (
        "HealthResponse",
        "MeResponse",
        "PreferencesOut",
        "PreferencesPatch",
        "SourcesResponse",
        "FeedPage",
        "FeedItemOut",
        "ReadStateUpdate",
        "ReadStateOut",
        "BookmarkOut",
        "BookmarkPage",
    ):
        assert name in component_schemas, f"missing component schema {name}"
