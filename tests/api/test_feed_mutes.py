"""Muted words and tags — the feed predicate and the preference round trip.

Muting is :func:`app.services.feed._text_match` negated, so most of what
makes the matching correct is already covered by
``tests/api/test_feed_search.py`` and is not repeated. What is here is what
negation changes.

The difference that matters is the direction of a mistake. A search that
matches too little shows the reader an empty page they asked for and can
undo by typing something else. A mute that matches too much removes items
from a feed *silently and permanently*, with nothing on screen to say
which ones or why — so the tests that earn their place are the ones about
over-matching: an empty term, a metacharacter, a mute leaking into
bookmarks, and a mute surviving a topic it no longer names.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.models import (
    Bookmark,
    FeedItem,
    FeedItemTopic,
    MuteKind,
    Source,
    Topic,
    User,
    UserMutedTerm,
)
from tests.api.conftest import BASE_TIME, count_statements

CORPUS: list[tuple[str, str, str | None, list[str]]] = [
    ("derby", "Late winner settles the derby", "Two goals in stoppage time.", ["uk-news"]),
    ("league", "Premier League clubs agree a new deal", None, ["uk-news"]),
    ("premier", "A premier example of cache invalidation", None, ["webdev"]),
    ("keyset", "Keyset pagination in Postgres", "Why OFFSET degrades with depth.", ["webdev"]),
    ("snake", "Naming things in snake_case", None, ["webdev"]),
    ("snake-decoy", "Naming things in snakeXcase", None, ["webdev"]),
    ("untagged", "An item nobody classified", "Carries no topics at all.", []),
]


def _titles(payload: dict[str, Any]) -> list[str]:
    return [item["title"] for item in payload["items"]]


@pytest.fixture
def corpus(db_session: Session, catalogue: Any) -> dict[str, int]:
    """Items on top of the shared catalogue, so its topics already exist."""
    source = Source(
        slug="mutable",
        name="Mutable",
        feed_url="https://mutable.example/rss",
        website_url="https://mutable.example",
    )
    db_session.add(source)
    db_session.flush()
    topics: dict[str, int] = {
        row.slug: row.id for row in db_session.execute(select(Topic.slug, Topic.id))
    }

    seeded: dict[str, int] = {}
    for index, (key, title, summary, topic_slugs) in enumerate(CORPUS):
        item = FeedItem(
            source_id=source.id,
            canonical_url=f"https://mutable.example/{key}",
            title=title,
            summary=summary,
            published_at=BASE_TIME + timedelta(hours=1, minutes=index),
        )
        db_session.add(item)
        db_session.flush()
        seeded[key] = item.id
        db_session.add_all(
            FeedItemTopic(feed_item_id=item.id, topic_id=topics[slug]) for slug in topic_slugs
        )
    db_session.commit()
    return seeded


def _mute(client: TestClient, **patch: Any) -> dict[str, Any]:
    response = client.patch("/api/v1/me/preferences", json=patch)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _feed(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get("/api/v1/feed", params={"limit": 100, "sources": ["mutable"], **params})
    assert response.status_code == 200, response.text
    return dict(response.json())


# --- words ---------------------------------------------------------------


def test_a_muted_word_removes_its_items(authed_client: TestClient, corpus: dict[str, int]) -> None:
    _mute(authed_client, muted_words=["derby"])

    assert "Late winner settles the derby" not in _titles(_feed(authed_client))
    assert "Keyset pagination in Postgres" in _titles(_feed(authed_client))


def test_muting_nothing_removes_nothing(authed_client: TestClient, corpus: dict[str, int]) -> None:
    assert len(_titles(_feed(authed_client))) == len(CORPUS)


def test_unmuting_brings_the_items_back(authed_client: TestClient, corpus: dict[str, int]) -> None:
    """An explicit empty list clears, which is the contract `topics` and
    `sources` already have."""
    _mute(authed_client, muted_words=["derby"])
    assert len(_titles(_feed(authed_client))) == len(CORPUS) - 1

    _mute(authed_client, muted_words=[])
    assert len(_titles(_feed(authed_client))) == len(CORPUS)


def test_a_multi_word_mute_requires_all_its_words(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """ "premier league" hides the league and not every item mentioning a
    premier. Muting on any-of would take the second item too, which is a
    reader losing an article about caches because they dislike football."""
    _mute(authed_client, muted_words=["premier league"])
    titles = _titles(_feed(authed_client))

    assert "Premier League clubs agree a new deal" not in titles
    assert "A premier example of cache invalidation" in titles


def test_several_muted_words_are_independent(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    _mute(authed_client, muted_words=["derby", "keyset"])
    titles = _titles(_feed(authed_client))

    assert "Late winner settles the derby" not in titles
    assert "Keyset pagination in Postgres" not in titles
    assert "A premier example of cache invalidation" in titles


def test_muting_matches_the_summary_as_well_as_the_title(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    _mute(authed_client, muted_words=["stoppage"])

    assert "Late winner settles the derby" not in _titles(_feed(authed_client))


# --- the ways a mute could match too much --------------------------------


@pytest.mark.parametrize("term", ["   ", "\t", " \n "])
def test_a_whitespace_term_is_refused_rather_than_stored(
    authed_client: TestClient, corpus: dict[str, int], term: str
) -> None:
    """An empty term is a substring of every item, so storing one would
    empty the feed with nothing on screen to say why — and it survives the
    schema, which bounds length and runs before normalisation."""
    response = authed_client.patch("/api/v1/me/preferences", json={"muted_words": [term]})

    assert response.status_code == 422
    assert len(_titles(_feed(authed_client))) == len(CORPUS)


@pytest.mark.parametrize("field", ["muted_words", "muted_tags"])
def test_a_whitespace_term_does_not_clear_the_list_it_arrived_in(
    authed_client: TestClient, corpus: dict[str, int], field: str
) -> None:
    """Refused rather than dropped, and this is the difference.

    Dropping made ``["  "]`` normalise to ``[]``, which is the wire form of
    "unmute everything" — so a request that looks like adding one mute
    silently removed every mute the reader had. The test above could not
    see it, because it starts from an empty list where dropping and
    clearing produce the same answer: a guard passing for a reason
    unrelated to its subject, which is what the rest of this file exists
    to catch. Found in review rather than here.
    """
    _mute(authed_client, muted_words=["derby"], muted_tags=["uk-news"])

    response = authed_client.patch("/api/v1/me/preferences", json={field: ["   "]})
    assert response.status_code == 422

    still = authed_client.get("/api/v1/me").json()["preferences"]
    assert still["muted_words"] == ["derby"]
    assert still["muted_tags"] == ["uk-news"]


def test_a_metacharacter_in_a_mute_is_a_literal(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """Without `autoescape`, muting `snake_case` also hides `snakeXcase` —
    and a wildcard in a mute removes items the reader never named."""
    _mute(authed_client, muted_words=["snake_case"])
    titles = _titles(_feed(authed_client))

    assert "Naming things in snake_case" not in titles
    assert "Naming things in snakeXcase" in titles


def test_a_mute_does_not_reach_bookmarks(
    authed_client: TestClient, corpus: dict[str, int], db_session: Session, test_user: User
) -> None:
    """A bookmark is an explicit "keep this" — the argument
    `prune_feed_items` already makes for exempting bookmarks from
    retention. A saved item vanishing because a word was muted a month
    later is the same surprise in a quieter form."""
    db_session.add(Bookmark(user_id=test_user.id, feed_item_id=corpus["derby"]))
    db_session.commit()
    _mute(authed_client, muted_words=["derby"])

    bookmarks = authed_client.get("/api/v1/bookmarks").json()

    assert [entry["item"]["title"] for entry in bookmarks["bookmarks"]] == [
        "Late winner settles the derby"
    ]


def test_an_untagged_item_survives_a_muted_tag(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """`NOT IN` over the topic subquery must not take items carrying no
    topics at all — the failure mode `_effective_topics` records for the
    positive direction, in its negative form."""
    _mute(authed_client, muted_tags=["uk-news"])

    assert "An item nobody classified" in _titles(_feed(authed_client))


# --- tags ----------------------------------------------------------------


def test_a_muted_tag_removes_items_carrying_it(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    _mute(authed_client, muted_tags=["uk-news"])
    titles = _titles(_feed(authed_client))

    assert "Late winner settles the derby" not in titles
    assert "Premier League clubs agree a new deal" not in titles
    assert "Keyset pagination in Postgres" in titles


def test_a_muted_tag_naming_no_topic_is_refused(authed_client: TestClient) -> None:
    """Unlike a word. A muted word the catalogue has never heard of is the
    point; a muted tag that matches nothing is a typo that would report
    success and mute nothing."""
    response = authed_client.patch("/api/v1/me/preferences", json={"muted_tags": ["not-a-topic"]})

    assert response.status_code == 422


def test_words_and_tags_are_separate_lists(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """Replacing one must not clear the other — they are one table and a
    delete scoped to the wrong column would take both."""
    _mute(authed_client, muted_words=["derby"], muted_tags=["webdev"])
    saved = _mute(authed_client, muted_words=["keyset"])

    assert saved["muted_words"] == ["keyset"]
    assert saved["muted_tags"] == ["webdev"]


# --- normalisation -------------------------------------------------------


def test_terms_are_normalised_and_deduplicated(authed_client: TestClient) -> None:
    saved = _mute(
        authed_client, muted_words=["Football", "football", "  premier   league  ", "FOOTBALL"]
    )

    assert saved["muted_words"] == ["football", "premier league"]


def test_a_mute_is_case_insensitive_against_the_item(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    _mute(authed_client, muted_words=["DERBY"])

    assert "Late winner settles the derby" not in _titles(_feed(authed_client))


def test_muting_the_same_term_twice_is_one_row(
    authed_client: TestClient, db_session: Session, test_user: User
) -> None:
    """The composite primary key's promise, asserted against the table
    rather than against the response that reads it back."""
    _mute(authed_client, muted_words=["derby", "Derby", "  derby "])

    rows = db_session.scalars(
        select(UserMutedTerm.term).where(
            UserMutedTerm.user_id == test_user.id, UserMutedTerm.kind == MuteKind.WORD
        )
    ).all()

    assert list(rows) == ["derby"]


def test_a_term_over_the_length_bound_is_refused(authed_client: TestClient) -> None:
    response = authed_client.patch("/api/v1/me/preferences", json={"muted_words": ["x" * 65]})

    assert response.status_code == 422


def test_more_terms_than_the_cap_is_refused(authed_client: TestClient) -> None:
    response = authed_client.patch(
        "/api/v1/me/preferences", json={"muted_words": [f"term{n}" for n in range(101)]}
    )

    assert response.status_code == 422


# --- composition and cost ------------------------------------------------


def test_muting_composes_with_search(authed_client: TestClient, corpus: dict[str, int]) -> None:
    """A muted item stays muted when it is searched for. Deliberate: a mute
    is a standing preference and a search is a view over what it leaves —
    the alternative makes "why is this back?" the harder question."""
    _mute(authed_client, muted_words=["derby"])

    assert _titles(_feed(authed_client, q="derby")) == []


def test_muting_composes_with_the_read_filter(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    _mute(authed_client, muted_tags=["uk-news"])
    titles = _titles(_feed(authed_client, read_state="unread"))

    assert "Late winner settles the derby" not in titles
    assert "Keyset pagination in Postgres" in titles


def test_pages_stay_full_under_a_mute(authed_client: TestClient, corpus: dict[str, int]) -> None:
    """The keyset argument, in its negative form: the LIMIT is taken after
    the mute, so a page of two is two *surviving* items rather than two
    rows of which one is missing."""
    _mute(authed_client, muted_words=["derby"])

    first = _feed(authed_client, limit=2)
    assert len(first["items"]) == 2
    assert first["next_cursor"] is not None

    seen = list(_titles(first))
    cursor = first["next_cursor"]
    while cursor:
        page = _feed(authed_client, limit=2, cursor=cursor)
        seen.extend(_titles(page))
        cursor = page["next_cursor"]

    assert "Late winner settles the derby" not in seen
    assert len(seen) == len(CORPUS) - 1


def test_muting_costs_one_statement_whatever_the_page_size(
    authed_client: TestClient, corpus: dict[str, int], engine: Engine
) -> None:
    """The mute lookup is per request, not per card — the N+1 guard the
    base query carries, extended to the predicate that reads a table of
    its own."""
    _mute(authed_client, muted_words=["derby"], muted_tags=["webdev"])

    with count_statements(engine) as small:
        authed_client.get("/api/v1/feed", params={"limit": 1})
    with count_statements(engine) as large:
        authed_client.get("/api/v1/feed", params={"limit": 100})

    assert len(large) == len(small)
    assert sum("user_muted_terms" in sql for sql in large) == 1


# --- what the Codex review on PR #34 found -------------------------------


def test_a_term_that_grows_past_the_column_under_case_folding_is_refused(
    authed_client: TestClient,
) -> None:
    """``casefold`` is not length-preserving. Sixty-four ``ß`` satisfy the
    schema's ``max_length`` and become a hundred and twenty-eight ``s``
    before anything stores them — past ``VARCHAR(64)``, so PostgreSQL
    raises and SQLite quietly accepts a row that then fails validation on
    the way back out. Either way a valid-looking request is a 500.

    The bound is therefore checked again *after* normalising, where the
    value that will actually be stored exists. Refused rather than
    truncated: a shortened mute matches more than the reader asked for,
    and silently.

    **The status alone is not the assertion**, because on SQLite this
    returned 422 before the check existed and did so for a reason that
    had nothing to do with the input: ``_to_out`` failed validating its
    own *response*, and ``pydantic.ValidationError`` subclasses
    ``ValueError``, which the route maps to 422. The body said "1
    validation error for PreferencesOut" — an internal name, about the
    wrong model, describing a value the client never sent. And on
    PostgreSQL the write reaches ``VARCHAR(64)`` first and raises
    ``DataError``, which is not a ``ValueError`` and is therefore a 500.
    So the message is asserted too.
    """
    response = authed_client.patch("/api/v1/me/preferences", json={"muted_words": ["ß" * 64]})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "64" in detail
    assert "PreferencesOut" not in detail


def test_a_term_that_stays_inside_the_column_after_folding_is_kept(
    authed_client: TestClient,
) -> None:
    """The other side of the bound, so the check above cannot pass by
    refusing everything."""
    saved = _mute(authed_client, muted_words=["ß" * 30])

    assert saved["muted_words"] == ["ss" * 30]


def test_a_muted_topic_survives_the_topic_being_disabled(
    authed_client: TestClient, corpus: dict[str, int], db_session: Session
) -> None:
    """An operator retiring a topic must not strand a mute.

    The predicate matches slugs and does not consult ``topics.enabled``, so
    a disabled topic goes on hiding items. The catalogue stops returning
    it, so the client cannot show a control for it. Validating a patch
    against *enabled* topics then completes the trap: the mute keeps
    working, and any patch carrying it — which is every patch that changes
    a different mute, since the field is replace-the-whole-list — is a 422.
    """
    _mute(authed_client, muted_tags=["uk-news", "webdev"])
    topic = db_session.scalars(select(Topic).where(Topic.slug == "uk-news")).one()
    topic.enabled = False
    db_session.commit()

    # Still hidden: the mute did not stop working.
    assert "Late winner settles the derby" not in _titles(_feed(authed_client))

    # And still changeable, which is the half that was broken.
    saved = _mute(authed_client, muted_tags=["uk-news"])
    assert saved["muted_tags"] == ["uk-news"]

    unmuted = _mute(authed_client, muted_tags=[])
    assert unmuted["muted_tags"] == []
    assert "Late winner settles the derby" in _titles(_feed(authed_client))
