"""PUT /items/{item_id}/read-state — idempotent in both directions."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import UserReadItem
from tests.api.conftest import Catalogue


def _read_rows(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(UserReadItem)) or 0


def test_marking_read_returns_the_new_state(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    item_id = catalogue.items["hn-1"]

    response = authed_client.put(f"/api/v1/items/{item_id}/read-state", json={"read": True})

    assert response.status_code == 200
    assert response.json()["item_id"] == item_id
    assert response.json()["read"] is True
    assert response.json()["read_at"] is not None


def test_marking_read_twice_is_idempotent(
    authed_client: TestClient, db_session: Session, catalogue: Catalogue
) -> None:
    """Not merely "does not error": the second call must not restamp
    ``read_at`` either, or a double click rewrites history."""
    item_id = catalogue.items["hn-1"]

    first = authed_client.put(f"/api/v1/items/{item_id}/read-state", json={"read": True}).json()
    second = authed_client.put(f"/api/v1/items/{item_id}/read-state", json={"read": True}).json()

    assert first == second
    assert _read_rows(db_session) == 1


def test_marking_unread_removes_the_state(
    authed_client: TestClient, db_session: Session, catalogue: Catalogue
) -> None:
    item_id = catalogue.items["hn-1"]
    authed_client.put(f"/api/v1/items/{item_id}/read-state", json={"read": True})

    response = authed_client.put(f"/api/v1/items/{item_id}/read-state", json={"read": False})

    assert response.status_code == 200
    assert response.json() == {"item_id": item_id, "read": False, "read_at": None}
    assert _read_rows(db_session) == 0


def test_marking_unread_twice_is_idempotent(
    authed_client: TestClient, db_session: Session, catalogue: Catalogue
) -> None:
    item_id = catalogue.items["hn-1"]

    first = authed_client.put(f"/api/v1/items/{item_id}/read-state", json={"read": False})
    second = authed_client.put(f"/api/v1/items/{item_id}/read-state", json={"read": False})

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert _read_rows(db_session) == 0


def test_read_then_unread_then_read_again(authed_client: TestClient, catalogue: Catalogue) -> None:
    item_id = catalogue.items["hn-2"]

    for read in (True, False, True, True, False):
        response = authed_client.put(f"/api/v1/items/{item_id}/read-state", json={"read": read})
        assert response.status_code == 200
        assert response.json()["read"] is read


def test_unknown_item_is_404(authed_client: TestClient, catalogue: Catalogue) -> None:
    response = authed_client.put("/api/v1/items/999999/read-state", json={"read": True})

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown item"


def test_read_state_is_visible_in_the_feed(authed_client: TestClient, catalogue: Catalogue) -> None:
    item_id = catalogue.items["hn-1"]
    authed_client.put(f"/api/v1/items/{item_id}/read-state", json={"read": True})

    payload = authed_client.get("/api/v1/feed", params={"limit": 100}).json()
    card = next(item for item in payload["items"] if item["id"] == item_id)

    assert card["read"] is True
