"""GET /feed?read_state — narrowing the feed by the caller's read state.

The filter is a WHERE predicate over the ``user_read_items`` LEFT JOIN the
feed query already carries, so the three claims worth measuring are that
the default did not move, that each value narrows to the right set, and
that the predicate composes — with the other filters, with the saved
selection, with another user's read rows, and above all with the keyset
cursor. The last is the one that a plausible wrong implementation (filter
the page in Python, or in the client) passes every test but the paging
one, while returning short pages and a cursor naming a row the caller
never saw.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.schemas import PreferencesPatch
from app.db.models import FeedItem, Source, User, UserReadItem
from app.services import preferences as preferences_service
from tests.api.conftest import BASE_TIME, Catalogue


def _ids(payload: dict[str, Any]) -> list[int]:
    return [item["id"] for item in payload["items"]]


def _mark_read(db_session: Session, user: User, item_ids: list[int]) -> None:
    """Record read rows directly.

    The route is exercised by ``test_the_route_that_writes_read_state_is
    _what_the_filter_reads``; everywhere else this is fixture setup, and
    going through HTTP for it would make the arrangement of a fifteen-item
    fixture the slowest part of the suite.
    """
    db_session.add_all(UserReadItem(user_id=user.id, feed_item_id=item_id) for item_id in item_ids)
    db_session.commit()


# --- the default did not move -------------------------------------------


def test_absent_read_state_returns_read_and_unread_items(
    authed_client: TestClient, db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    """The regression guard. An existing client sends no ``read_state``
    and must see exactly what it saw before the parameter existed."""
    _mark_read(db_session, test_user, [catalogue.items["hn-1"], catalogue.items["lobsters"]])

    payload = authed_client.get("/api/v1/feed", params={"limit": 100}).json()

    assert _ids(payload) == catalogue.visible_newest_first
    by_id = {item["id"]: item["read"] for item in payload["items"]}
    assert by_id[catalogue.items["hn-1"]] is True
    assert by_id[catalogue.items["hn-0"]] is False


def test_read_state_all_is_the_same_as_omitting_the_parameter(
    authed_client: TestClient, db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    """``all`` is in the vocabulary so a client that spells the default
    explicitly is not answered with a 422."""
    _mark_read(db_session, test_user, [catalogue.items["hn-1"]])

    explicit = authed_client.get("/api/v1/feed", params={"limit": 100, "read_state": "all"}).json()
    omitted = authed_client.get("/api/v1/feed", params={"limit": 100}).json()

    assert _ids(explicit) == _ids(omitted) == catalogue.visible_newest_first


# --- each value narrows to the right set --------------------------------


def test_unread_only_excludes_read_items(
    authed_client: TestClient, db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    read = [catalogue.items["hn-1"], catalogue.items["bbc-a"], catalogue.items["untagged"]]
    _mark_read(db_session, test_user, read)

    payload = authed_client.get(
        "/api/v1/feed", params={"limit": 100, "read_state": "unread"}
    ).json()

    expected = [item for item in catalogue.visible_newest_first if item not in read]
    assert _ids(payload) == expected
    assert all(item["read"] is False for item in payload["items"])


def test_read_only_excludes_unread_items(
    authed_client: TestClient, db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    read = [catalogue.items["hn-1"], catalogue.items["bbc-a"], catalogue.items["untagged"]]
    _mark_read(db_session, test_user, read)

    payload = authed_client.get("/api/v1/feed", params={"limit": 100, "read_state": "read"}).json()

    expected = [item for item in catalogue.visible_newest_first if item in read]
    assert _ids(payload) == expected
    assert all(item["read"] is True for item in payload["items"])


def test_the_two_values_partition_the_unfiltered_feed(
    authed_client: TestClient, db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    """No item is in both halves and none is in neither.

    ``read_at`` is NOT NULL in the model, so "has a row" and "has a
    non-null ``read_at``" cannot diverge — but the predicate is written
    against the column rather than the join, and this is what would catch
    the two drifting apart.
    """
    _mark_read(db_session, test_user, [catalogue.items["hn-1"], catalogue.items["bbc-a"]])

    everything = set(_ids(authed_client.get("/api/v1/feed", params={"limit": 100}).json()))
    unread = set(
        _ids(
            authed_client.get("/api/v1/feed", params={"limit": 100, "read_state": "unread"}).json()
        )
    )
    read = set(
        _ids(authed_client.get("/api/v1/feed", params={"limit": 100, "read_state": "read"}).json())
    )

    assert unread | read == everything
    assert unread & read == set()


def test_unread_only_with_nothing_read_is_the_whole_feed(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    """The outer join must stay an outer join: an item with no read row
    at all is unread, not absent."""
    payload = authed_client.get(
        "/api/v1/feed", params={"limit": 100, "read_state": "unread"}
    ).json()

    assert _ids(payload) == catalogue.visible_newest_first


def test_read_only_with_nothing_read_is_empty(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    payload = authed_client.get("/api/v1/feed", params={"limit": 100, "read_state": "read"}).json()

    assert payload["items"] == []
    assert payload["next_cursor"] is None


def test_the_route_that_writes_read_state_is_what_the_filter_reads(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    """End to end through HTTP, once: mark read, and watch the item cross
    from one half to the other."""
    item_id = catalogue.items["hn-3"]
    authed_client.put(f"/api/v1/items/{item_id}/read-state", json={"read": True})

    unread = _ids(
        authed_client.get("/api/v1/feed", params={"limit": 100, "read_state": "unread"}).json()
    )
    read = _ids(
        authed_client.get("/api/v1/feed", params={"limit": 100, "read_state": "read"}).json()
    )
    assert item_id not in unread
    assert read == [item_id]

    authed_client.put(f"/api/v1/items/{item_id}/read-state", json={"read": False})

    back = _ids(
        authed_client.get("/api/v1/feed", params={"limit": 100, "read_state": "unread"}).json()
    )
    assert item_id in back


@pytest.mark.parametrize("value", ["", "yes", "true", "1", "Unread", "unread,read"])
def test_an_unrecognised_read_state_is_rejected(authed_client: TestClient, value: str) -> None:
    """422 rather than a silent widening: a value we do not understand is
    a client bug, and answering it with the unfiltered feed would hide it."""
    response = authed_client.get("/api/v1/feed", params={"read_state": value})

    assert response.status_code == 422


# --- composition with the other filters ---------------------------------


def test_read_filter_composes_with_a_source_filter(
    authed_client: TestClient, db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    _mark_read(db_session, test_user, [catalogue.items["bbc-a"], catalogue.items["hn-1"]])

    payload = authed_client.get(
        "/api/v1/feed", params={"limit": 100, "sources": ["bbc-news"], "read_state": "unread"}
    ).json()

    # Not merely "excludes bbc-a": hn-1 is read *and* out of the source
    # filter, so an implementation applying only one of the two predicates
    # shows up here rather than passing on a subset.
    assert _ids(payload) == [catalogue.items["bbc-b"]]


def test_read_filter_composes_with_a_topic_filter(
    authed_client: TestClient, db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    _mark_read(db_session, test_user, [catalogue.items["hn-1"], catalogue.items["hn-4"]])

    payload = authed_client.get(
        "/api/v1/feed", params={"limit": 100, "topics": ["webdev"], "read_state": "read"}
    ).json()

    assert _ids(payload) == [catalogue.items["hn-4"], catalogue.items["hn-1"]]


def test_read_filter_composes_with_the_saved_selection(
    authed_client: TestClient, db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    """``read_state`` is orthogonal to the saved-selection fallback: an
    absent ``sources`` still means "use what I saved"."""
    preferences_service.apply_patch(
        db_session, test_user, PreferencesPatch(sources=["bbc-news"], topics=[])
    )
    db_session.commit()
    _mark_read(db_session, test_user, [catalogue.items["bbc-a"], catalogue.items["lobsters"]])

    payload = authed_client.get("/api/v1/feed", params={"limit": 100, "read_state": "read"}).json()

    assert _ids(payload) == [catalogue.items["bbc-a"]]


def test_another_users_read_rows_do_not_narrow_my_feed(
    sign_in: Any, db_session: Session, test_user: User, second_user: User, catalogue: Catalogue
) -> None:
    """``user_id`` is pinned inside the join's ON clause, so the predicate
    reads only the caller's rows. Without that pin, one user marking an
    item read would remove it from everyone's unread feed."""
    _mark_read(db_session, second_user, list(catalogue.items.values()))

    mine = sign_in(test_user)
    payload = mine.get("/api/v1/feed", params={"limit": 100, "read_state": "unread"}).json()

    assert _ids(payload) == catalogue.visible_newest_first
    assert (
        mine.get("/api/v1/feed", params={"limit": 100, "read_state": "read"}).json()["items"] == []
    )


# --- composition with the cursor ----------------------------------------


@dataclass
class Interleaved:
    """Fifteen items alternating read and unread, newest first."""

    read: list[int]
    unread: list[int]

    @property
    def everything(self) -> list[int]:
        return sorted(self.read + self.unread, reverse=True)


@pytest.fixture
def interleaved(db_session: Session, test_user: User) -> Interleaved:
    """A feed where read and unread items alternate.

    Alternating is the point rather than the count: with every third item
    read, a page boundary in the *filtered* listing falls between two
    matching rows that are not adjacent in the unfiltered one. A filter
    applied after the LIMIT — in the service or in the client — returns
    short pages and mints a cursor from the last row of the *unfiltered*
    page, which then skips the matches between there and the next page.
    """
    source = Source(
        slug="ticker",
        name="Ticker",
        feed_url="https://ticker.example/rss",
        website_url="https://ticker.example",
    )
    db_session.add(source)
    db_session.flush()

    read: list[int] = []
    unread: list[int] = []
    for index in range(15):
        item = FeedItem(
            source_id=source.id,
            canonical_url=f"https://ticker.example/{index}",
            title=f"Ticker {index}",
            published_at=BASE_TIME + timedelta(minutes=index),
        )
        db_session.add(item)
        db_session.flush()
        (read if index % 3 == 0 else unread).append(item.id)
    db_session.commit()

    _mark_read(db_session, test_user, read)
    return Interleaved(read=read, unread=unread)


def _walk(client: TestClient, params: dict[str, Any], limit: int) -> tuple[list[int], list[int]]:
    """Page to exhaustion; return the ids seen and each page's size."""
    seen: list[int] = []
    sizes: list[int] = []
    cursor: str | None = None
    for _ in range(50):
        page_params = dict(params, limit=limit)
        if cursor is not None:
            page_params["cursor"] = cursor
        payload = client.get("/api/v1/feed", params=page_params).json()
        seen.extend(_ids(payload))
        sizes.append(len(payload["items"]))
        cursor = payload["next_cursor"]
        if cursor is None:
            return seen, sizes
    raise AssertionError("cursor never exhausted")


def _assert_pages_are_full(sizes: list[int], limit: int) -> None:
    """Every page but the last holds exactly ``limit`` items.

    A property of the predicate being in the WHERE: the statement asks for
    ``limit + 1`` *matching* rows, so a short page can only mean the
    listing ran out. Post-filtering — dropping rows from a page the
    database already limited — cannot hold this, which is the point.
    Asserted at every page size rather than once, because the sizes are
    already in hand and the failure it catches is otherwise invisible: the
    ids come back complete and in order either way.
    """
    assert all(size == limit for size in sizes[:-1]), f"ragged pages at limit={limit}: {sizes}"
    assert sizes[-1] <= limit


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 7])
def test_paging_an_unread_feed_yields_every_match_exactly_once(
    authed_client: TestClient, interleaved: Interleaved, limit: int
) -> None:
    seen, sizes = _walk(authed_client, {"read_state": "unread"}, limit)

    expected = sorted(interleaved.unread, reverse=True)
    assert seen == expected, "paging lost, repeated, or reordered a match"
    assert not set(seen) & set(interleaved.read), "a read item survived the filter"
    _assert_pages_are_full(sizes, limit)


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 7])
def test_paging_a_read_feed_yields_every_match_exactly_once(
    authed_client: TestClient, interleaved: Interleaved, limit: int
) -> None:
    seen, sizes = _walk(authed_client, {"read_state": "read"}, limit)

    assert seen == sorted(interleaved.read, reverse=True)
    assert not set(seen) & set(interleaved.unread)
    _assert_pages_are_full(sizes, limit)


def test_pages_of_a_filtered_feed_stay_full(
    authed_client: TestClient, interleaved: Interleaved
) -> None:
    """The property that separates a WHERE predicate from a post-filter.

    Ten of the fifteen items are unread, so at ``limit=4`` the pages must
    be 4, 4, 2. Filtering after the LIMIT gives 3, 3, 2, 2 instead —
    ragged pages, more round trips, and an infinite-scroll sentinel that
    fires against a page it has already filled.
    """
    seen, sizes = _walk(authed_client, {"read_state": "unread"}, 4)

    assert len(seen) == 10
    assert sizes == [4, 4, 2]


def test_the_cursor_of_a_filtered_page_names_a_matching_row(
    authed_client: TestClient, interleaved: Interleaved
) -> None:
    """A cursor minted from a row the filter excludes would skip whatever
    matches between it and the next page. Measured by walking the same
    listing twice at different page sizes and requiring the same order."""
    fine, _ = _walk(authed_client, {"read_state": "unread"}, 1)
    coarse, _ = _walk(authed_client, {"read_state": "unread"}, 6)

    assert fine == coarse == sorted(interleaved.unread, reverse=True)


def test_the_read_filter_survives_a_publication_time_tie(
    authed_client: TestClient, db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    """The two BBC items share ``published_at``; marking one read must
    remove exactly that one, at a boundary a one-per-page walk puts
    between them."""
    _mark_read(db_session, test_user, [catalogue.items["bbc-a"]])

    seen, _sizes = _walk(authed_client, {"read_state": "unread"}, 1)

    assert catalogue.items["bbc-a"] not in seen
    assert catalogue.items["bbc-b"] in seen
    assert len(seen) == len(catalogue.visible_newest_first) - 1


def test_filtering_does_not_add_a_query_per_item(
    db_session: Session, engine: Any, test_user: User, interleaved: Interleaved
) -> None:
    """The filter rides the join that was already there; it must not turn
    into a lookup per card."""
    from app.api.v1.schemas.feed import ReadFilter
    from app.services import feed as feed_service
    from tests.api.conftest import count_statements

    with count_statements(engine) as small:
        feed_service.get_feed_page(db_session, test_user, read_state=ReadFilter.UNREAD, limit=1)
    with count_statements(engine) as large:
        feed_service.get_feed_page(db_session, test_user, read_state=ReadFilter.UNREAD, limit=10)

    assert len(large) == len(small)
    assert len(large) <= 4
