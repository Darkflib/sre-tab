"""GET /feed?q= — narrowing, composition, and the two engine paths.

The claim under test is not "search works". It is that search is *one
more predicate in the same statement*, which is what keeps pages full and
cursors honest, and which is the reason this went in as a query parameter
rather than as a second endpoint. So the interesting tests here are the
ones about composition and pagination, not the ones about matching.

Everything runs on SQLite, which is the ``LIKE`` half of
:func:`app.services.feed.search_predicate`. The PostgreSQL half — and the
GIN index it exists for — is in ``tests/postgres/test_search.py``, which
asserts the query plan rather than the results, because a mismatched
index expression is built without error and then never used.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.models import FeedItem, Source, User, UserReadItem
from tests.api.conftest import BASE_TIME, count_statements

#: Titles and summaries chosen so each assertion below has exactly one
#: reason to pass. ``Postgres``/``postgres`` carry the case question.
#:
#: The last four are a matched pair each, and they exist because the first
#: version of the metacharacter tests below passed with ``autoescape``
#: removed — they asserted that a literal search finds its literal, which
#: is true either way. A wildcard is only visible against something it
#: would wrongly reach, so each metacharacter item has a decoy differing
#: from it exactly where ``%`` or ``_`` would match anything: escaped, the
#: search finds one; unescaped, it finds both.
CORPUS: list[tuple[str, str, str | None]] = [
    ("keyset", "Keyset pagination in Postgres", "Why OFFSET degrades with depth."),
    ("gin", "A GIN index for full-text search", "Postgres builds it happily and may never use it."),
    ("summary-only", "An unremarkable headline", "The word quokka appears only down here."),
    ("football", "Late winner settles the derby", "Two goals in stoppage time."),
    ("percent", "Cut latency by 100%overnight", None),
    ("percent-decoy", "Cut latency by 100% overnight", None),
    ("snake", "Naming things in snake_case", "A note on identifiers."),
    ("snake-decoy", "Naming things in snakeXcase", "A note on identifiers."),
]


def _ids(payload: dict[str, Any]) -> list[str]:
    """Response ids mapped back to their corpus keys, newest first."""
    return [item["title"] for item in payload["items"]]


@pytest.fixture
def corpus(db_session: Session) -> dict[str, int]:
    """One source, six items, one minute apart in corpus order."""
    source = Source(
        slug="searchable",
        name="Searchable",
        feed_url="https://searchable.example/rss",
        website_url="https://searchable.example",
    )
    db_session.add(source)
    db_session.flush()

    seeded: dict[str, int] = {}
    for index, (key, title, summary) in enumerate(CORPUS):
        item = FeedItem(
            source_id=source.id,
            canonical_url=f"https://searchable.example/{key}",
            title=title,
            summary=summary,
            published_at=BASE_TIME + timedelta(minutes=index),
        )
        db_session.add(item)
        db_session.flush()
        seeded[key] = item.id
    db_session.commit()
    return seeded


def _search(client: TestClient, query: str, **params: Any) -> dict[str, Any]:
    response = client.get("/api/v1/feed", params={"q": query, **params})
    assert response.status_code == 200, response.text
    return dict(response.json())


# --- matching ------------------------------------------------------------


def test_search_narrows_to_matching_items(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    payload = _search(authed_client, "pagination")

    assert _ids(payload) == ["Keyset pagination in Postgres"]


def test_search_is_case_insensitive(authed_client: TestClient, corpus: dict[str, int]) -> None:
    lower = _search(authed_client, "postgres")
    upper = _search(authed_client, "POSTGRES")

    assert _ids(lower) == _ids(upper)
    assert len(_ids(lower)) == 2


def test_search_matches_the_summary_as_well_as_the_title(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """The document is title *and* summary; a title-only match would pass
    every other test in this file."""
    payload = _search(authed_client, "quokka")

    assert _ids(payload) == ["An unremarkable headline"]


def test_multiple_terms_are_all_required(authed_client: TestClient, corpus: dict[str, int]) -> None:
    """Two words mean both, not either — the ``plainto_tsquery`` semantics
    the SQLite branch is written to agree with."""
    both = _search(authed_client, "index search")
    neither_together = _search(authed_client, "keyset quokka")

    assert _ids(both) == ["A GIN index for full-text search"]
    assert _ids(neither_together) == []


def test_search_orders_by_publication_time_not_relevance(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """Newest first, as every other listing is. The cursor is
    ``(published_at, id)``; a relevance order would need a different one."""
    payload = _search(authed_client, "postgres")

    assert _ids(payload) == [
        "A GIN index for full-text search",
        "Keyset pagination in Postgres",
    ]


# --- the empty query -----------------------------------------------------


@pytest.mark.parametrize("query", ["", "   ", "\t\n"])
def test_a_blank_query_narrows_nothing(
    authed_client: TestClient, corpus: dict[str, int], query: str
) -> None:
    """A cleared search box returns the feed, never an empty page. Absent,
    empty, and whitespace-only are deliberately one answer."""
    payload = _search(authed_client, query)

    assert len(payload["items"]) == len(CORPUS)


def test_a_query_over_the_length_bound_is_rejected(authed_client: TestClient) -> None:
    response = authed_client.get("/api/v1/feed", params={"q": "x" * 201})

    assert response.status_code == 422


def test_more_terms_than_the_cap_still_answers(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """The cap truncates rather than erroring, and the terms that survive
    still narrow — a query of twenty words must not 500 or return the
    unfiltered feed."""
    payload = _search(authed_client, "pagination " + " ".join(f"w{n}" for n in range(30)))

    assert _ids(payload) == []


# --- LIKE metacharacters -------------------------------------------------


def test_percent_is_a_literal_not_a_wildcard(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """``100%overnight`` is one string to a reader and "100, then anything,
    then overnight" to LIKE. The decoy is the second reading; only the
    first may come back."""
    payload = _search(authed_client, "100%overnight")

    assert _ids(payload) == ["Cut latency by 100%overnight"]


def test_underscore_is_a_literal_not_a_single_character_wildcard(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """Same shape, and the commoner one in this corpus: an identifier a
    developer would actually type into a search box."""
    payload = _search(authed_client, "snake_case")

    assert _ids(payload) == ["Naming things in snake_case"]


# --- composition, which is the point -------------------------------------


def test_search_composes_with_the_source_filter(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    matching = _search(authed_client, "postgres", sources=["searchable"])
    elsewhere = _search(authed_client, "postgres", sources=["hacker-news"])

    assert len(_ids(matching)) == 2
    assert _ids(elsewhere) == []


def test_search_composes_with_the_read_filter(
    authed_client: TestClient, corpus: dict[str, int], db_session: Session, test_user: User
) -> None:
    db_session.add(UserReadItem(user_id=test_user.id, feed_item_id=corpus["gin"]))
    db_session.commit()

    assert _ids(_search(authed_client, "postgres", read_state="unread")) == [
        "Keyset pagination in Postgres"
    ]
    assert _ids(_search(authed_client, "postgres", read_state="read")) == [
        "A GIN index for full-text search"
    ]


def test_search_does_not_reach_items_from_a_disabled_source(
    authed_client: TestClient, corpus: dict[str, int], db_session: Session
) -> None:
    """The source gate is in the base query and search must not step around
    it — an item that leaves the feed must not come back through the search
    box."""
    source = db_session.scalars(select(Source).where(Source.slug == "searchable")).one()
    source.enabled = False
    db_session.commit()

    assert _search(authed_client, "postgres")["items"] == []


# --- pagination stays honest ---------------------------------------------


def test_pages_stay_full_and_the_cursor_names_a_matching_row(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """Two matches, a page size of one. If the predicate were applied to
    the page after it came back rather than inside the statement, the first
    page here would be ragged and the cursor would name a row the caller
    never saw."""
    first = _search(authed_client, "postgres", limit=1)
    assert _ids(first) == ["A GIN index for full-text search"]
    assert first["next_cursor"] is not None

    second = _search(authed_client, "postgres", limit=1, cursor=first["next_cursor"])
    assert _ids(second) == ["Keyset pagination in Postgres"]
    assert second["next_cursor"] is None


def test_search_adds_no_statements_per_card(
    authed_client: TestClient, corpus: dict[str, int], engine: Engine
) -> None:
    """Two statements a page, searched or not — the N+1 guard the base
    query carries must survive the new predicate."""
    with count_statements(engine) as recorded:
        authed_client.get("/api/v1/feed", params={"q": "postgres"})
    searched = [sql for sql in recorded if "feed_items" in sql]

    assert len(searched) == 2
