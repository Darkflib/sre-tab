"""Cross-user isolation — v1 acceptance criterion 4.

Not a rounding error in the API contract: one user's bookmarks and read
state must be unreachable from another's session, including by guessing
item ids. Every test here has user B attempt something against an item
user A has state on, and asserts both that B sees nothing and that A's
state is unharmed.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.schemas import PreferencesPatch
from app.db.models import User
from app.services import preferences as preferences_service
from tests.api.conftest import Catalogue

SignIn = Callable[[User], TestClient]


def test_bookmarks_are_not_listed_to_another_user(
    sign_in: SignIn, test_user: User, second_user: User, catalogue: Catalogue
) -> None:
    item_id = catalogue.items["hn-1"]
    sign_in(test_user).put(f"/api/v1/items/{item_id}/bookmark")

    payload = sign_in(second_user).get("/api/v1/bookmarks").json()

    assert payload["bookmarks"] == []


def test_another_user_cannot_delete_a_bookmark(
    sign_in: SignIn, test_user: User, second_user: User, catalogue: Catalogue
) -> None:
    """B guessing A's item id gets a 204 — its own no-op delete — and A
    keeps the bookmark. Answering 404 instead would confirm the guess."""
    item_id = catalogue.items["hn-1"]
    created = sign_in(test_user).put(f"/api/v1/items/{item_id}/bookmark").json()

    response = sign_in(second_user).delete(f"/api/v1/items/{item_id}/bookmark")

    assert response.status_code == 204
    still_there = sign_in(test_user).get("/api/v1/bookmarks").json()["bookmarks"]
    assert [entry["item"]["id"] for entry in still_there] == [item_id]
    assert still_there[0]["created_at"] == created["created_at"]


def test_another_users_bookmark_does_not_leak_into_the_feed(
    sign_in: SignIn, test_user: User, second_user: User, catalogue: Catalogue
) -> None:
    item_id = catalogue.items["hn-1"]
    sign_in(test_user).put(f"/api/v1/items/{item_id}/bookmark")

    payload = sign_in(second_user).get("/api/v1/feed", params={"limit": 100}).json()
    card = next(item for item in payload["items"] if item["id"] == item_id)

    assert card["bookmarked"] is False


def test_bookmarking_the_same_item_gives_each_user_their_own(
    sign_in: SignIn, test_user: User, second_user: User, catalogue: Catalogue
) -> None:
    item_id = catalogue.items["hn-1"]
    sign_in(test_user).put(f"/api/v1/items/{item_id}/bookmark")
    sign_in(second_user).put(f"/api/v1/items/{item_id}/bookmark")

    sign_in(second_user).delete(f"/api/v1/items/{item_id}/bookmark")

    assert sign_in(second_user).get("/api/v1/bookmarks").json()["bookmarks"] == []
    kept = sign_in(test_user).get("/api/v1/bookmarks").json()["bookmarks"]
    assert [entry["item"]["id"] for entry in kept] == [item_id]


def test_read_state_does_not_leak_into_another_users_feed(
    sign_in: SignIn, test_user: User, second_user: User, catalogue: Catalogue
) -> None:
    item_id = catalogue.items["hn-1"]
    sign_in(test_user).put(f"/api/v1/items/{item_id}/read-state", json={"read": True})

    payload = sign_in(second_user).get("/api/v1/feed", params={"limit": 100}).json()
    card = next(item for item in payload["items"] if item["id"] == item_id)

    assert card["read"] is False


def test_another_user_cannot_clear_read_state(
    sign_in: SignIn, test_user: User, second_user: User, catalogue: Catalogue
) -> None:
    item_id = catalogue.items["hn-1"]
    sign_in(test_user).put(f"/api/v1/items/{item_id}/read-state", json={"read": True})

    sign_in(second_user).put(f"/api/v1/items/{item_id}/read-state", json={"read": False})

    payload = sign_in(test_user).get("/api/v1/feed", params={"limit": 100}).json()
    card = next(item for item in payload["items"] if item["id"] == item_id)
    assert card["read"] is True


def test_another_user_cannot_set_read_state(
    sign_in: SignIn, test_user: User, second_user: User, catalogue: Catalogue
) -> None:
    item_id = catalogue.items["hn-1"]

    sign_in(second_user).put(f"/api/v1/items/{item_id}/read-state", json={"read": True})

    payload = sign_in(test_user).get("/api/v1/feed", params={"limit": 100}).json()
    card = next(item for item in payload["items"] if item["id"] == item_id)
    assert card["read"] is False


def test_read_state_response_describes_only_the_caller(
    sign_in: SignIn, test_user: User, second_user: User, catalogue: Catalogue
) -> None:
    """B reading its own state back must not see A's ``read_at``."""
    item_id = catalogue.items["hn-1"]
    sign_in(test_user).put(f"/api/v1/items/{item_id}/read-state", json={"read": True})

    body = (
        sign_in(second_user).put(f"/api/v1/items/{item_id}/read-state", json={"read": False}).json()
    )

    assert body == {"item_id": item_id, "read": False, "read_at": None}


def test_preferences_are_per_user(
    sign_in: SignIn,
    db_session: Session,
    test_user: User,
    second_user: User,
    catalogue: Catalogue,
) -> None:
    """A's saved source selection must not narrow B's feed."""
    preferences_service.apply_patch(db_session, test_user, PreferencesPatch(sources=["lobsters"]))
    db_session.commit()

    mine = sign_in(test_user).get("/api/v1/feed", params={"limit": 100}).json()
    theirs = sign_in(second_user).get("/api/v1/feed", params={"limit": 100}).json()

    assert [item["id"] for item in mine["items"]] == [catalogue.items["lobsters"]]
    assert len(theirs["items"]) == len(catalogue.visible_newest_first)
