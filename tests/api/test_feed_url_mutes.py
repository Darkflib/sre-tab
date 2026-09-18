"""Muting by URL — the reduction, the feed predicate, and the round trip.

A different predicate from the word and tag mutes in
``tests/api/test_feed_mutes.py``, and the file that one's docstring
argues for applies here with more force: a URL mute that matches too
much removes a *whole site* silently. So most of what is here is about
where a term stops — ``jrandom`` against ``jrandom2``, ``medium.com``
against ``medium.com.evil.example``, and a ``%`` or ``_`` that a ``LIKE``
would read as a wildcard.

The corpus lives in one source, and that is deliberate rather than
economical. It is an aggregator, so its items link to many hosts — which
is the case a URL mute exists for, because the source filter cannot say
"Lobsters, but not the Medium links".

``tests/postgres/test_url_mutes.py`` runs :data:`CORPUS` and
:data:`MUTED` against PostgreSQL, so the two engines are held to one set
of survivors.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.v1.schemas.me import MAX_MUTED_TERMS
from app.db.models import (
    MAX_MUTED_TERM_LENGTH,
    Bookmark,
    FeedItem,
    MuteKind,
    Source,
    User,
    UserMutedTerm,
)
from app.ingest.normalise import MAX_URL_LENGTH
from app.services.preferences import url_mute_term
from tests.api.conftest import BASE_TIME, count_statements

#: Keyed by what each row is there to prove. Every URL is in the form
#: ``normalise_url`` stores, because that is the only form the predicate
#: ever meets.
CORPUS: dict[str, str] = {
    "author-post": "https://dev.to/jrandom/first-post-1abc",
    "author-root": "https://dev.to/jrandom",
    "author-query": "https://dev.to/jrandom?page=2",
    "author-shouted": "https://dev.to/JRandom/shouty-post-3ghi",
    # The equality case, shouted. It began as the one row SQLite could tell
    # `lower()` was missing from, back when the match was a `LIKE` that
    # ignored ASCII case on SQLite; it stays as the boundary's own witness.
    "author-root-shouted": "https://dev.to/JRANDOM",
    "author-http": "http://dev.to/jrandom/plain-post-4jkl",
    "author-www": "https://www.dev.to/jrandom/www-post-5mno",
    "author-neighbour": "https://dev.to/jrandom2/other-post-2def",
    "author-prefix-of-segment": "https://dev.to/jrandomly/musings",
    "medium": "https://medium.com/@someone/a-story-6pqr",
    "medium-lookalike": "https://medium.com.evil.example/phish",
    "medium-subdomain": "https://alice.medium.com/a-story",
    "percent": "https://pct.example/100%/offer",
    "percent-decoy": "https://pct.example/100x/offer",
    "underscore": "https://under.example/snake_case/post",
    "underscore-decoy": "https://under.example/snakeXcase/post",
    "section": "https://www.theguardian.com/football/2026/sep/18/match-report",
    "section-neighbour": "https://www.theguardian.com/footballers/2026/sep/18/profile",
    "discussion": "https://lobste.rs/s/abc123/a_discussion",
}

#: One of each shape of term: an author, a bare host, a section on a
#: ``www.`` host, and two whose segment carries a ``LIKE`` metacharacter —
#: kept although the match no longer uses ``LIKE``, so that going back to
#: one cannot quietly reintroduce wildcards.
MUTED = [
    "dev.to/jrandom",
    "medium.com",
    "pct.example/100%",
    "theguardian.com/football",
    "under.example/snake_case",
]

#: What :data:`MUTED` must leave standing, and nothing else.
SURVIVORS = {
    "author-neighbour",
    "author-prefix-of-segment",
    "medium-lookalike",
    "medium-subdomain",
    "percent-decoy",
    "underscore-decoy",
    "section-neighbour",
    "discussion",
}


@pytest.fixture
def corpus(db_session: Session) -> dict[str, int]:
    source = Source(
        slug="lobsters",
        name="Lobsters",
        feed_url="https://lobste.rs/rss",
        website_url="https://lobste.rs/",
    )
    db_session.add(source)
    db_session.flush()
    seeded: dict[str, int] = {}
    for index, (key, url) in enumerate(CORPUS.items()):
        item = FeedItem(
            source_id=source.id,
            canonical_url=url,
            title=key,
            published_at=BASE_TIME + timedelta(minutes=index),
        )
        db_session.add(item)
        db_session.flush()
        seeded[key] = item.id
    db_session.commit()
    return seeded


def _save(client: TestClient, **patch: Any) -> dict[str, Any]:
    response = client.patch("/api/v1/me/preferences", json=patch)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _visible(client: TestClient) -> set[str]:
    response = client.get("/api/v1/feed", params={"limit": 100, "sources": ["lobsters"]})
    assert response.status_code == 200, response.text
    return {item["title"] for item in response.json()["items"]}


def _hidden_by(client: TestClient, *terms: str) -> set[str]:
    _save(client, muted_urls=list(terms))
    return set(CORPUS) - _visible(client)


# --- where a term stops --------------------------------------------------


def test_the_corpus_is_all_visible_before_anything_is_muted(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """Without this, every "is hidden" below could pass against a feed that
    shows nothing."""
    assert _visible(authed_client) == set(CORPUS)


def test_an_author_mute_takes_the_author_and_nothing_beside_it(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """The motivating case. ``jrandom2`` and ``jrandomly`` are other people,
    and a raw string prefix would take both."""
    assert _hidden_by(authed_client, "dev.to/jrandom") == {
        "author-post",
        "author-root",
        "author-query",
        "author-shouted",
        "author-root-shouted",
        "author-http",
        "author-www",
    }


def test_a_bare_host_is_a_host_and_not_a_string_prefix(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """``medium.com.evil.example`` begins with ``medium.com`` and is not
    Medium. ``alice.medium.com`` *is* Medium's, and is still not covered: a
    term names one host exactly, which is stated rather than discovered."""
    assert _hidden_by(authed_client, "medium.com") == {"medium"}


def test_a_section_mute_matches_through_www(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """Every Guardian link is ``www.theguardian.com``, and the stored term
    is not — ``link_host`` removed it — so without the ``www.`` variant
    this mute would save and never match."""
    assert _hidden_by(authed_client, "theguardian.com/football") == {"section"}


@pytest.mark.parametrize(
    ("term", "hidden"),
    [("pct.example/100%", {"percent"}), ("under.example/snake_case", {"underscore"})],
)
def test_a_like_metacharacter_in_a_term_is_a_literal(
    authed_client: TestClient, corpus: dict[str, int], term: str, hidden: set[str]
) -> None:
    """Unescaped, ``100%`` matches ``100x`` and ``snake_case`` matches
    ``snakeXcase``. Both characters survive into a stored term — ``%``
    when it is not a valid escape, ``_`` always."""
    assert _hidden_by(authed_client, term) == hidden


def test_every_shape_together_leaves_exactly_the_survivors(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """The per-case tests above in one statement, which is how the feed
    actually meets them."""
    assert set(CORPUS) - _hidden_by(authed_client, *MUTED) == SURVIVORS


def test_a_url_mute_reaches_through_an_aggregator(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """What neither the source filter nor a tag can say: Lobsters, but not
    its Medium links. The discussion on Lobsters' own host stays."""
    _save(authed_client, muted_urls=["medium.com"])
    visible = _visible(authed_client)

    assert "medium" not in visible
    assert "discussion" in visible


def test_another_readers_url_mute_does_not_reach_this_feed(
    authed_client: TestClient, corpus: dict[str, int], db_session: Session, second_user: User
) -> None:
    """The subquery reads ``user_muted_terms`` itself, so the user it is
    pinned to is the only thing keeping one reader's mutes out of another's
    feed. This reader mutes something too, or the predicate would not be
    built at all and the test would pass for the wrong reason."""
    db_session.add(UserMutedTerm(user_id=second_user.id, kind=MuteKind.URL, term="dev.to/jrandom"))
    db_session.commit()

    assert _hidden_by(authed_client, "medium.com") == {"medium"}


def test_a_muted_word_that_looks_like_a_site_is_still_a_word(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """Three kinds share the table, and the subquery must ask for one. A
    word mute of ``dev.to`` matches text, which nothing here carries; read
    as a URL term it would take every dev.to link."""
    _save(authed_client, muted_words=["dev.to"], muted_urls=["medium.com"])

    assert set(CORPUS) - _visible(authed_client) == {"medium"}


def test_a_url_mute_does_not_reach_bookmarks(
    authed_client: TestClient, corpus: dict[str, int], db_session: Session, test_user: User
) -> None:
    """The exemption ``mute_predicates`` records for words and tags, and a
    third kind must not quietly end it."""
    db_session.add(Bookmark(user_id=test_user.id, feed_item_id=corpus["medium"]))
    db_session.commit()
    _save(authed_client, muted_urls=["medium.com"])

    bookmarks = authed_client.get("/api/v1/bookmarks").json()

    assert [entry["item"]["title"] for entry in bookmarks["bookmarks"]] == ["medium"]


def test_unmuting_brings_the_items_back(authed_client: TestClient, corpus: dict[str, int]) -> None:
    _save(authed_client, muted_urls=["dev.to/jrandom"])
    _save(authed_client, muted_urls=[])

    assert _visible(authed_client) == set(CORPUS)


# --- what arrives, and what is stored ------------------------------------


def test_a_pasted_article_url_is_reduced_to_what_it_names(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """Scheme, ``www.``, query, fragment, and everything past the first
    segment go; the response says what was kept, so the reader sees the
    reduction rather than discovering it."""
    saved = _save(
        authed_client,
        muted_urls=[
            "https://www.TheGuardian.com/football/2026/sep/18/match-report?CMP=share#comments"
        ],
    )

    assert saved["muted_urls"] == ["theguardian.com/football"]
    assert "section" not in _visible(authed_client)


def test_a_url_longer_than_a_term_is_accepted_on_the_way_in(authed_client: TestClient) -> None:
    """The bug the separate input bound exists for. Validated at the term's
    sixty-four characters, this is a 422 before the reduction runs."""
    pasted = "https://dev.to/jrandom/" + "a-very-long-slug-" * 20
    assert len(pasted) > MAX_MUTED_TERM_LENGTH

    assert _save(authed_client, muted_urls=[pasted])["muted_urls"] == ["dev.to/jrandom"]


def test_a_url_past_the_input_bound_is_refused(authed_client: TestClient) -> None:
    response = authed_client.patch(
        "/api/v1/me/preferences",
        json={"muted_urls": ["https://dev.to/" + "a" * MAX_URL_LENGTH]},
    )

    assert response.status_code == 422


def test_a_reduction_that_still_does_not_fit_is_refused_and_says_so(
    authed_client: TestClient,
) -> None:
    """Refused rather than truncated: a shortened segment is a different
    segment, and could be somebody else's. The existing list survives, for
    the reason ``test_a_whitespace_term_does_not_clear_the_list_it_arrived_in``
    gives."""
    _save(authed_client, muted_urls=["medium.com"])

    response = authed_client.patch(
        "/api/v1/me/preferences",
        json={"muted_urls": ["medium.com", "https://example.com/" + "x" * 60 + "/post"]},
    )

    assert response.status_code == 422
    assert str(MAX_MUTED_TERM_LENGTH) in response.json()["detail"]
    assert authed_client.get("/api/v1/me").json()["preferences"]["muted_urls"] == ["medium.com"]


@pytest.mark.parametrize(
    "raw",
    [
        "   ",
        "https://127.0.0.1/admin",
        "https://[::1]/admin",
        "https://user:secret@dev.to/jrandom",
        "ftp://dev.to/jrandom",
        "localhost/jrandom",
        "https://dev.to:8443/jrandom",
        "https://example.com//jrandom",
    ],
)
def test_a_url_the_column_could_never_match_is_refused(authed_client: TestClient, raw: str) -> None:
    """Each of these would otherwise store a term that matches nothing, or
    — the last — the whole host."""
    response = authed_client.patch("/api/v1/me/preferences", json={"muted_urls": [raw]})

    assert response.status_code == 422


def test_a_refusal_does_not_echo_credentials(authed_client: TestClient) -> None:
    response = authed_client.patch(
        "/api/v1/me/preferences", json={"muted_urls": ["https://user:secret@dev.to/jrandom"]}
    )

    assert "secret" not in response.text


def test_stored_terms_are_the_reduced_ones_and_one_row_each(
    authed_client: TestClient, db_session: Session, test_user: User
) -> None:
    """Three pastes of one author are one mute. Asserted against the table,
    not against the response that reads it back."""
    _save(
        authed_client,
        muted_urls=[
            "dev.to/jrandom",
            "https://dev.to/jrandom/first-post-1abc",
            "HTTP://WWW.DEV.TO/JRandom/?utm_source=feed",
        ],
    )

    rows = db_session.scalars(
        select(UserMutedTerm.term).where(
            UserMutedTerm.user_id == test_user.id, UserMutedTerm.kind == MuteKind.URL
        )
    ).all()

    assert list(rows) == ["dev.to/jrandom"]


def test_sending_the_stored_list_back_changes_nothing(authed_client: TestClient) -> None:
    """The client sends every existing term back on each save, and the
    server reduces each again — so a stored term must reduce to itself."""
    first = _save(
        authed_client,
        muted_urls=["https://www.theguardian.com/football/x", "medium.com", "pct.example/100%"],
    )["muted_urls"]

    assert _save(authed_client, muted_urls=first)["muted_urls"] == first


def test_urls_words_and_tags_are_separate_lists(authed_client: TestClient, catalogue: Any) -> None:
    """One table, three kinds, and a replace scoped to the wrong column
    would take the others with it."""
    _save(authed_client, muted_words=["derby"], muted_tags=["webdev"], muted_urls=["medium.com"])
    saved = _save(authed_client, muted_urls=["dev.to/jrandom"])

    assert saved["muted_words"] == ["derby"]
    assert saved["muted_tags"] == ["webdev"]
    assert saved["muted_urls"] == ["dev.to/jrandom"]


def test_more_urls_than_the_cap_is_refused(authed_client: TestClient) -> None:
    response = authed_client.patch(
        "/api/v1/me/preferences",
        json={"muted_urls": [f"host{n}.example" for n in range(101)]},
    )

    assert response.status_code == 422


def test_url_mutes_are_read_per_request_not_per_card(
    authed_client: TestClient, corpus: dict[str, int], engine: Engine
) -> None:
    """The N+1 guard in ``test_feed_mutes.py``, for the kind that reads its
    terms inside the feed's own statement. Two statements name the table —
    the lookup of which kinds exist, and the feed query carrying the
    correlated ``EXISTS`` — and the count must not grow with the page."""
    _save(authed_client, muted_words=["derby"], muted_urls=MUTED)

    with count_statements(engine) as small:
        authed_client.get("/api/v1/feed", params={"limit": 1})
    with count_statements(engine) as large:
        authed_client.get("/api/v1/feed", params={"limit": 100})

    assert len(large) == len(small)
    assert sum("user_muted_terms" in sql for sql in large) == 2


# --- the reduction on its own --------------------------------------------


@pytest.mark.parametrize(
    ("raw", "term"),
    [
        ("dev.to/jrandom", "dev.to/jrandom"),
        ("dev.to/jrandom/", "dev.to/jrandom"),
        ("//dev.to/jrandom", "dev.to/jrandom"),
        ("https://dev.to/jrandom/first-post-1abc?utm_source=x#top", "dev.to/jrandom"),
        ("https://dev.to/jrandom?page=2", "dev.to/jrandom"),
        ("medium.com", "medium.com"),
        ("https://Medium.com/", "medium.com"),
        ("www.medium.com", "medium.com"),
        ("https://medium.com/@someone/a-story", "medium.com/@someone"),
        ("https://news.ycombinator.com/item?id=1", "news.ycombinator.com/item"),
        ("https://bücher.example/Straße/x", "xn--bcher-kva.example/stra%c3%9fe"),
        ("under.example/snake_case", "under.example/snake_case"),
    ],
)
def test_the_reduction(raw: str, term: str) -> None:
    assert url_mute_term(raw) == term


@pytest.mark.parametrize("raw", [*CORPUS.values(), *MUTED, "https://bücher.example/Straße/x"])
def test_the_reduction_is_idempotent(raw: str) -> None:
    """The property ``test_sending_the_stored_list_back_changes_nothing``
    depends on, over every URL shape in this file."""
    term = url_mute_term(raw)

    assert url_mute_term(term) == term


def test_a_host_that_is_www_twice_over_is_refused() -> None:
    """``link_host`` strips one ``www.``, so storing ``www.example.com``
    would reduce to ``example.com`` on the next save — the one host shape
    where the reduction is not idempotent, refused rather than special-cased."""
    with pytest.raises(ValueError, match="www"):
        url_mute_term("https://www.www.example.com/x")


def test_the_feed_answers_with_every_kind_at_its_cap(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """The cap is a promise that a list that long works, so it is asked of
    a real feed query rather than only of the 422 one past it.

    From the Codex review on PR #47. The first URL predicate bound twelve
    clauses a term into one flat ``OR``, and SQLite refuses an expression
    tree deeper than a thousand: from about 84 URL mutes, every feed
    request was a 500, for a list the API had accepted. The test beside
    it, a hundred and one refused, could not see that."""
    _save(
        authed_client,
        muted_words=[f"word{n}" for n in range(MAX_MUTED_TERMS)],
        muted_urls=[f"host{n}.example/author" for n in range(MAX_MUTED_TERMS)],
    )

    assert _visible(authed_client) == set(CORPUS)
