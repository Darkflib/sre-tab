"""openapi.json is served, complete, and matches the PRD endpoint table.

It also has to match the copy the frontend compiles against. See
``test_committed_contract_matches_the_live_schema`` for why that is a
separate claim.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.main import create_app
from app.settings import Settings

#: The frontend builds against a committed copy of this document rather than
#: a running server, and ``src/api/schema.d.ts`` is generated from that copy.
CONTRACT = Path(__file__).resolve().parents[1] / "frontend" / "openapi.json"


def _as_committed(spec: dict[str, Any]) -> bytes:
    """Serialise *spec* exactly as frontend/README.md's command writes it.

    ``json.dumps`` at indent 2 through ``print``, trailing newline included.
    Matching the documented procedure byte for byte is the point: a test that
    re-serialised the document its own way would compare two things neither
    of which is what a regeneration produces.

    Bytes rather than ``str``, and the caller reads with ``read_bytes``,
    because ``Path.read_text`` applies universal-newline translation — it
    would decode a CRLF file to the same string as an LF one and report the
    two as equal. There is no ``.gitattributes`` pinning line endings here,
    so a checkout with ``core.autocrlf`` is the way that happens. Saying
    "byte for byte" and then comparing translated text is the kind of
    almost-true claim this file exists to stop.
    """
    return (json.dumps(spec, indent=2) + "\n").encode("utf-8")


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


def test_committed_contract_matches_the_live_schema(app: FastAPI) -> None:
    """The document the frontend compiles against is the one the app serves.

    ``frontend/openapi.json`` is committed so the build never needs a running
    server or a Python toolchain, and ``src/api/schema.d.ts`` is generated
    from it. Regenerating both is a manual step, and until this test existed
    the only thing enforcing it was a sentence in frontend/README.md asking
    for discipline.

    The failure it guards against is silent in both directions. A contract
    change nobody regenerates leaves the client typed against a server that
    no longer exists, and ``tsc`` goes on passing — because it is checking
    the client against the stale copy, faithfully.
    """
    assert CONTRACT.read_bytes() == _as_committed(app.openapi()), (
        "frontend/openapi.json no longer matches the live schema. Regenerate "
        "both artefacts in one commit (frontend/README.md):\n"
        '  LOG_LEVEL=CRITICAL uv run python -c "import json; from app.main '
        'import create_app; print(json.dumps(create_app().openapi(), indent=2))"'
        " > frontend/openapi.json\n"
        "  cd frontend && npm run generate:api"
    )


def test_the_contract_does_not_depend_on_configuration(settings: Settings, engine: Engine) -> None:
    """Which is what lets the test above compare against fixture settings.

    The regeneration command calls ``create_app()`` bare, so it builds the
    application from whatever the environment holds; the test above builds it
    from the deterministic fixture. Those are the same document only because
    nothing settings-dependent reaches the schema — true by construction
    today, and cheap enough to keep measured rather than assumed.
    """
    baseline = _as_committed(create_app(settings, engine=engine).openapi())

    # A clean checkout has no .env and every setting carries a default, so
    # this is what the documented command actually runs with in CI.
    defaults = Settings(_env_file=None)  # type: ignore[call-arg]  # pydantic-settings init kwarg
    assert _as_committed(create_app(defaults, engine=engine).openapi()) == baseline

    # ROADMAP.md claims the flag governs the UI and not the contract, which
    # is why /api/v1/openapi.json is served either way. Measured here.
    with_docs = defaults.model_copy(update={"docs_enabled": True})
    assert _as_committed(create_app(with_docs, engine=engine).openapi()) == baseline


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
