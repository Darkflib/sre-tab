"""GET /feed — ordering, keyset pagination, filters, and bounds."""

from __future__ import annotations

import base64
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.api.v1.schemas import PreferencesPatch
from app.db.models import FeedItem, User
from app.services import feed as feed_service
from app.services import preferences as preferences_service
from tests.api.conftest import BASE_TIME, Catalogue, count_statements


def _ids(payload: dict[str, Any]) -> list[int]:
    return [item["id"] for item in payload["items"]]


def test_feed_returns_visible_items_newest_first(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    payload = authed_client.get("/api/v1/feed").json()

    assert _ids(payload) == catalogue.visible_newest_first
    assert payload["next_cursor"] is None


def test_feed_hides_items_from_disabled_sources(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    payload = authed_client.get("/api/v1/feed").json()

    assert catalogue.items["retired"] not in _ids(payload)


def test_feed_card_carries_source_topics_and_user_state(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    payload = authed_client.get("/api/v1/feed", params={"limit": 100}).json()
    card = next(item for item in payload["items"] if item["id"] == catalogue.items["hn-0"])

    assert card["source"] == {"slug": "hacker-news", "name": "Hacker News", "icon_url": None}
    assert card["topics"] == ["webdev"]
    assert card["read"] is False
    assert card["bookmarked"] is False
    assert card["published_at"] == "2026-08-01T12:00:00Z"


def test_default_page_size_is_25(authed_client: TestClient, db_session: Session) -> None:
    """Thirty items, no ``limit``: the contract's default must bound it."""
    from app.db.models import Source

    source = Source(
        slug="bulk", name="Bulk", feed_url="https://bulk.example/rss", website_url="https://bulk"
    )
    db_session.add(source)
    db_session.flush()
    db_session.add_all(
        FeedItem(
            source_id=source.id,
            canonical_url=f"https://bulk.example/{index}",
            title=f"Bulk {index}",
            published_at=BASE_TIME + timedelta(minutes=index),
        )
        for index in range(30)
    )
    db_session.commit()

    payload = authed_client.get("/api/v1/feed").json()

    assert len(payload["items"]) == 25
    assert payload["next_cursor"] is not None


def test_pagination_walks_to_exhaustion_without_gaps_or_repeats(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    expected = catalogue.visible_newest_first
    seen: list[int] = []
    cursor: str | None = None
    pages = 0

    while True:
        params: dict[str, Any] = {"limit": 3}
        if cursor is not None:
            params["cursor"] = cursor
        payload = authed_client.get("/api/v1/feed", params=params).json()
        pages += 1
        seen.extend(_ids(payload))
        cursor = payload["next_cursor"]
        if cursor is None:
            break
        assert pages < 20, "cursor never exhausted"

    assert seen == expected
    assert len(set(seen)) == len(seen)


def test_pagination_is_stable_across_a_publication_time_tie(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    """The two BBC items share ``published_at``.

    Paging one at a time puts the cursor boundary exactly between them,
    which is where an ordering keyed on the timestamp alone loses or
    duplicates a row.
    """
    seen: list[int] = []
    cursor: str | None = None

    for _ in range(len(catalogue.visible_newest_first)):
        params: dict[str, Any] = {"limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        payload = authed_client.get("/api/v1/feed", params=params).json()
        seen.extend(_ids(payload))
        cursor = payload["next_cursor"]

    tied = {catalogue.items["bbc-a"], catalogue.items["bbc-b"]}
    assert tied.issubset(seen)
    assert seen == catalogue.visible_newest_first
    assert cursor is None


def test_filter_by_source_slug(authed_client: TestClient, catalogue: Catalogue) -> None:
    payload = authed_client.get("/api/v1/feed", params={"sources": ["bbc-news"]}).json()

    assert set(_ids(payload)) == {catalogue.items["bbc-a"], catalogue.items["bbc-b"]}


def test_filter_by_multiple_source_slugs(authed_client: TestClient, catalogue: Catalogue) -> None:
    payload = authed_client.get("/api/v1/feed", params={"sources": ["bbc-news", "lobsters"]}).json()

    assert set(_ids(payload)) == {
        catalogue.items["bbc-a"],
        catalogue.items["bbc-b"],
        catalogue.items["lobsters"],
    }


def test_filter_by_topic_slug(authed_client: TestClient, catalogue: Catalogue) -> None:
    payload = authed_client.get("/api/v1/feed", params={"topics": ["python"]}).json()

    assert _ids(payload) == [catalogue.items["lobsters"]]


def test_explicit_topic_filter_excludes_unclassified_items(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    """An explicit narrowing is literal — no topics means no match."""
    payload = authed_client.get("/api/v1/feed", params={"topics": ["webdev"]}).json()

    assert catalogue.items["untagged"] not in _ids(payload)
    assert catalogue.items["hn-0"] in _ids(payload)


def test_unknown_filter_slug_yields_an_empty_page(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    """Not a fallback to "everything": a filter nothing satisfies is
    empty, otherwise a typo silently widens the feed."""
    payload = authed_client.get("/api/v1/feed", params={"sources": ["does-not-exist"]}).json()

    assert payload["items"] == []
    assert payload["next_cursor"] is None


def test_feed_falls_back_to_the_saved_source_selection(
    authed_client: TestClient, db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    preferences_service.apply_patch(db_session, test_user, PreferencesPatch(sources=["lobsters"]))
    db_session.commit()

    payload = authed_client.get("/api/v1/feed").json()

    assert _ids(payload) == [catalogue.items["lobsters"]]


def test_feed_falls_back_to_the_saved_topic_selection(
    authed_client: TestClient, db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    # Sources cleared so only the topic selection narrows; the default
    # profile would otherwise also pin the source set.
    preferences_service.apply_patch(
        db_session, test_user, PreferencesPatch(topics=["uk-news"], sources=[])
    )
    db_session.commit()

    payload = authed_client.get("/api/v1/feed").json()

    assert set(_ids(payload)) == {catalogue.items["bbc-a"], catalogue.items["bbc-b"]}


def test_a_selection_covering_every_topic_narrows_nothing(
    authed_client: TestClient, db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    """Including the item ingest failed to classify.

    Defaults select every enabled topic; if that were applied as a filter,
    an unclassified item would vanish from a source the user explicitly
    enabled, which reads as data loss rather than as a filter.
    """
    preferences_service.ensure_profile(db_session, test_user)
    db_session.commit()

    payload = authed_client.get("/api/v1/feed").json()

    assert catalogue.items["untagged"] in _ids(payload)


def test_cleared_selection_shows_the_instance_defaults(
    authed_client: TestClient, db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    preferences_service.apply_patch(db_session, test_user, PreferencesPatch(sources=[], topics=[]))
    db_session.commit()

    payload = authed_client.get("/api/v1/feed").json()

    assert _ids(payload) == catalogue.visible_newest_first


@pytest.mark.parametrize("limit", [0, -1, 101, 1000])
def test_page_size_bounds_are_enforced(authed_client: TestClient, limit: int) -> None:
    response = authed_client.get("/api/v1/feed", params={"limit": limit})

    assert response.status_code == 422


def test_maximum_page_size_is_accepted(authed_client: TestClient, catalogue: Catalogue) -> None:
    response = authed_client.get("/api/v1/feed", params={"limit": 100})

    assert response.status_code == 200


def _absurd_cursor() -> str:
    """A cursor whose microsecond field is far outside timedelta's range."""
    return base64.urlsafe_b64encode(f"1:{'9' * 400}:1".encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64 at all !!",
        "",
        "Zm9v",  # valid base64, "foo" — not our payload shape
        "OTk5OTk5",  # base64 of "999999" — one field, not three
        "Mjo4OTox",  # base64 of "2:89:1" — version we never issued
        "MTpub3QtYS1udW1iZXI6MQ",  # base64 of "1:not-a-number:1"
        # "1:999…:1" with 400 digits. int() parses it happily; the
        # timedelta multiplication that follows raises OverflowError,
        # which was not in the caught tuple and answered 500.
        _absurd_cursor(),
    ],
)
def test_malformed_cursor_is_rejected_cleanly(
    authed_client: TestClient, catalogue: Catalogue, cursor: str
) -> None:
    """400, never a 500: the cursor is a client-supplied opaque token."""
    response = authed_client.get("/api/v1/feed", params={"cursor": cursor})

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid cursor"


def test_cursor_is_opaque(authed_client: TestClient, catalogue: Catalogue) -> None:
    payload = authed_client.get("/api/v1/feed", params={"limit": 2}).json()
    cursor = payload["next_cursor"]

    assert cursor is not None
    # Nothing a client could parse and construct by hand: no separator, no
    # timestamp, no column name. Changing the encoding must not be a
    # breaking API change, which only holds while clients cannot read it.
    assert ":" not in cursor
    assert "2026-08-01" not in cursor
    assert "published_at" not in cursor


def test_read_and_bookmark_state_is_folded_in(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    item_id = catalogue.items["hn-3"]
    authed_client.put(f"/api/v1/items/{item_id}/read-state", json={"read": True})
    authed_client.put(f"/api/v1/items/{item_id}/bookmark")

    payload = authed_client.get("/api/v1/feed", params={"limit": 100}).json()
    card = next(item for item in payload["items"] if item["id"] == item_id)
    other = next(item for item in payload["items"] if item["id"] == catalogue.items["hn-4"])

    assert card["read"] is True
    assert card["bookmarked"] is True
    assert other["read"] is False
    assert other["bookmarked"] is False


def test_state_folding_does_not_scale_with_page_size(
    db_session: Session, engine: Engine, test_user: User, catalogue: Catalogue
) -> None:
    """The N+1 guard.

    ``lazy="raise"`` already makes a missed eager load fail loudly, but it
    says nothing about the per-user ``read``/``bookmarked`` flags — a
    lookup per card is valid SQLAlchemy and quietly quadratic. Statement
    count must be identical for a one-item page and a ten-item page.
    """
    with count_statements(engine) as small:
        feed_service.get_feed_page(db_session, test_user, limit=1)
    with count_statements(engine) as large:
        feed_service.get_feed_page(db_session, test_user, limit=10)

    assert len(large) == len(small)
    assert len(large) <= 4
