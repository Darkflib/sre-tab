"""Every one of the twelve PRD endpoints answers 501 until Phase 1 lands."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

# (method, path, request kwargs) for all twelve endpoints.
ENDPOINTS: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", "/api/v1/auth/github/start", {}),
    ("GET", "/api/v1/auth/github/callback", {"params": {"code": "x", "state": "y"}}),
    ("POST", "/api/v1/auth/logout", {}),
    ("GET", "/api/v1/me", {}),
    ("PATCH", "/api/v1/me/preferences", {"json": {}}),
    ("DELETE", "/api/v1/me", {}),
    ("GET", "/api/v1/sources", {}),
    ("GET", "/api/v1/feed", {}),
    ("PUT", "/api/v1/items/1/read-state", {"json": {"read": True}}),
    ("GET", "/api/v1/bookmarks", {}),
    ("PUT", "/api/v1/items/1/bookmark", {}),
    ("DELETE", "/api/v1/items/1/bookmark", {}),
]


@pytest.mark.parametrize(("method", "path", "kwargs"), ENDPOINTS)
def test_stub_returns_501_unauthenticated(
    client: TestClient, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    response = client.request(method, path, **kwargs)
    assert response.status_code == 501, f"{method} {path} -> {response.status_code}"


@pytest.mark.parametrize(("method", "path", "kwargs"), ENDPOINTS)
def test_stub_returns_501_authenticated(
    authed_client: TestClient, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    """With auth overridden the stub bodies themselves still answer 501."""
    response = authed_client.request(method, path, **kwargs)
    assert response.status_code == 501, f"{method} {path} -> {response.status_code}"
