"""Bookmark create, remove, and listing."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Bookmark, User
from tests.api.conftest import Catalogue


def _bookmark_rows(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Bookmark)) or 0


def test_put_creates_a_bookmark_with_the_full_card(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    item_id = catalogue.items["hn-1"]

    response = authed_client.put(f"/api/v1/items/{item_id}/bookmark")

    assert response.status_code == 200
    body = response.json()
    assert body["item"]["id"] == item_id
    assert body["item"]["source"]["slug"] == "hacker-news"
    assert body["item"]["topics"] == ["webdev"]
    assert body["item"]["bookmarked"] is True
    assert body["created_at"] is not None


def test_put_is_idempotent(
    authed_client: TestClient, db_session: Session, catalogue: Catalogue
) -> None:
    """A second save keeps the original ``created_at``: the listing is
    ordered by it, so restamping would silently reorder the user's list."""
    item_id = catalogue.items["hn-1"]

    first = authed_client.put(f"/api/v1/items/{item_id}/bookmark").json()
    second = authed_client.put(f"/api/v1/items/{item_id}/bookmark").json()

    assert first["created_at"] == second["created_at"]
    assert _bookmark_rows(db_session) == 1


def test_delete_removes_the_bookmark(
    authed_client: TestClient, db_session: Session, catalogue: Catalogue
) -> None:
    item_id = catalogue.items["hn-1"]
    authed_client.put(f"/api/v1/items/{item_id}/bookmark")

    response = authed_client.delete(f"/api/v1/items/{item_id}/bookmark")

    assert response.status_code == 204
    assert _bookmark_rows(db_session) == 0


def test_deleting_an_absent_bookmark_is_not_an_error(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    item_id = catalogue.items["hn-1"]

    first = authed_client.delete(f"/api/v1/items/{item_id}/bookmark")
    second = authed_client.delete(f"/api/v1/items/{item_id}/bookmark")

    assert first.status_code == 204
    assert second.status_code == 204


@pytest.mark.parametrize("method", ["put", "delete"])
def test_unknown_item_is_404(authed_client: TestClient, catalogue: Catalogue, method: str) -> None:
    response = authed_client.request(method.upper(), "/api/v1/items/999999/bookmark")

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown item"


def test_list_returns_the_users_bookmarks_newest_first(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    saved = [catalogue.items["hn-0"], catalogue.items["lobsters"], catalogue.items["bbc-a"]]
    for item_id in saved:
        authed_client.put(f"/api/v1/items/{item_id}/bookmark")

    payload = authed_client.get("/api/v1/bookmarks").json()
    listed = [entry["item"]["id"] for entry in payload["bookmarks"]]

    assert sorted(listed) == sorted(saved)
    assert payload["next_cursor"] is None
    assert all(entry["item"]["bookmarked"] is True for entry in payload["bookmarks"])


def test_list_is_empty_before_anything_is_saved(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    payload = authed_client.get("/api/v1/bookmarks").json()

    assert payload == {"bookmarks": [], "next_cursor": None}


def test_list_pagination_walks_to_exhaustion(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    saved = [catalogue.items[key] for key in ("hn-0", "hn-1", "hn-2", "hn-3", "lobsters")]
    for item_id in saved:
        authed_client.put(f"/api/v1/items/{item_id}/bookmark")

    seen: list[int] = []
    cursor: str | None = None
    for _ in range(len(saved) + 2):
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        payload = authed_client.get("/api/v1/bookmarks", params=params).json()
        seen.extend(entry["item"]["id"] for entry in payload["bookmarks"])
        cursor = payload["next_cursor"]
        if cursor is None:
            break

    assert cursor is None
    assert sorted(seen) == sorted(saved)
    assert len(set(seen)) == len(seen)


def test_list_pagination_is_stable_across_a_created_at_tie(
    authed_client: TestClient, db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    """Bookmarks written with identical ``created_at``.

    The API cannot produce this on its own — it stamps microseconds — but
    a bulk import or a restored backup can, and the id tiebreaker is what
    stops a page repeating or skipping when it does.
    """
    stamp = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)
    saved = [catalogue.items[key] for key in ("hn-0", "hn-1", "hn-2", "hn-3")]
    db_session.add_all(
        Bookmark(user_id=test_user.id, feed_item_id=item_id, created_at=stamp) for item_id in saved
    )
    db_session.commit()

    seen: list[int] = []
    cursor: str | None = None
    for _ in range(len(saved) + 2):
        params: dict[str, Any] = {"limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        payload = authed_client.get("/api/v1/bookmarks", params=params).json()
        seen.extend(entry["item"]["id"] for entry in payload["bookmarks"])
        cursor = payload["next_cursor"]
        if cursor is None:
            break

    assert cursor is None
    assert seen == sorted(saved, reverse=True)


def test_list_carries_read_state(authed_client: TestClient, catalogue: Catalogue) -> None:
    item_id = catalogue.items["hn-1"]
    authed_client.put(f"/api/v1/items/{item_id}/bookmark")
    authed_client.put(f"/api/v1/items/{item_id}/read-state", json={"read": True})

    payload = authed_client.get("/api/v1/bookmarks").json()

    assert payload["bookmarks"][0]["item"]["read"] is True


def test_bookmarks_survive_their_source_being_disabled(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    """Retiring a source removes it from the feed; it must not quietly
    empty someone's saved items."""
    item_id = catalogue.items["retired"]

    response = authed_client.put(f"/api/v1/items/{item_id}/bookmark")
    payload = authed_client.get("/api/v1/bookmarks").json()

    assert response.status_code == 200
    assert [entry["item"]["id"] for entry in payload["bookmarks"]] == [item_id]


def test_bookmark_state_is_visible_in_the_feed(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    item_id = catalogue.items["hn-1"]
    authed_client.put(f"/api/v1/items/{item_id}/bookmark")

    payload = authed_client.get("/api/v1/feed", params={"limit": 100}).json()
    card = next(item for item in payload["items"] if item["id"] == item_id)

    assert card["bookmarked"] is True


@pytest.mark.parametrize(
    "cursor",
    [
        "nonsense!!",
        "Zm9v",
        "Mjo4OTox",
        # Both listings share decode_cursor, so both inherited the
        # unhandled OverflowError on an absurd microsecond field.
        base64.urlsafe_b64encode(f"1:{'9' * 400}:1".encode()).decode().rstrip("="),
    ],
)
def test_malformed_cursor_is_rejected_cleanly(
    authed_client: TestClient, catalogue: Catalogue, cursor: str
) -> None:
    response = authed_client.get("/api/v1/bookmarks", params={"cursor": cursor})

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid cursor"


@pytest.mark.parametrize("limit", [0, 101])
def test_page_size_bounds_are_enforced(authed_client: TestClient, limit: int) -> None:
    response = authed_client.get("/api/v1/bookmarks", params={"limit": limit})

    assert response.status_code == 422
