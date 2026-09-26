"""Reading by language — the preference, the feed predicate, the card.

The property that matters most is the one that is easiest to lose:
**an item with no detected language is always shown.** ``NULL`` is what
the detector stores when it is not sure, and what every item carries
before ``sre-tab detect-languages`` has run, so a predicate that dropped
it would make "English only" hide English. Most of what is here is about
that, and about a language list never reaching further than its owner.

``tests/postgres/test_languages.py`` runs :data:`CORPUS` through the same
predicate on PostgreSQL.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.v1.schemas.me import MAX_LANGUAGES
from app.db.models import Bookmark, FeedItem, Source, User, UserPreferenceLanguage
from tests.api.conftest import BASE_TIME, count_statements

#: Title to stored language. The titles are keys, not text: the language
#: is written directly, because this file is about the predicate and not
#: the detector (``tests/ingest/test_language.py`` is about the detector).
CORPUS: dict[str, str | None] = {
    "english": "en",
    "english-too": "en",
    "portuguese": "pt",
    "thai": "th",
    "spanish": "es",
    "undetermined": None,
}


@pytest.fixture
def corpus(db_session: Session) -> dict[str, int]:
    source = Source(
        slug="dev-to", name="DEV", feed_url="https://dev.to/feed", website_url="https://dev.to/"
    )
    db_session.add(source)
    db_session.flush()
    seeded: dict[str, int] = {}
    for index, (title, language) in enumerate(CORPUS.items()):
        item = FeedItem(
            source_id=source.id,
            canonical_url=f"https://dev.to/someone/{title}",
            title=title,
            language=language,
            published_at=BASE_TIME + timedelta(minutes=index),
        )
        db_session.add(item)
        db_session.flush()
        seeded[title] = item.id
    db_session.commit()
    return seeded


def _save(client: TestClient, **patch: Any) -> dict[str, Any]:
    response = client.patch("/api/v1/me/preferences", json=patch)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _visible(client: TestClient) -> set[str]:
    response = client.get("/api/v1/feed", params={"limit": 100, "sources": ["dev-to"]})
    assert response.status_code == 200, response.text
    return {item["title"] for item in response.json()["items"]}


# --- the feed -----------------------------------------------------------


def test_no_languages_chosen_narrows_nothing(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """The default, and what every reader has after the upgrade. Without
    this, every "is hidden" below could pass against an empty feed."""
    assert _visible(authed_client) == set(CORPUS)


def test_english_only_keeps_english_and_the_undetermined(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    _save(authed_client, languages=["en"])

    assert _visible(authed_client) == {"english", "english-too", "undetermined"}


def test_an_undetermined_item_survives_any_choice(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    """The fail-open clause on its own: a list naming none of the corpus's
    languages still leaves the item nobody could classify."""
    _save(authed_client, languages=["de"])

    assert _visible(authed_client) == {"undetermined"}


def test_several_languages_are_a_union(authed_client: TestClient, corpus: dict[str, int]) -> None:
    _save(authed_client, languages=["en", "pt"])

    assert _visible(authed_client) == {"english", "english-too", "portuguese", "undetermined"}


def test_clearing_the_list_brings_everything_back(
    authed_client: TestClient, corpus: dict[str, int]
) -> None:
    _save(authed_client, languages=["en"])
    _save(authed_client, languages=[])

    assert _visible(authed_client) == set(CORPUS)


def test_another_readers_languages_do_not_reach_this_feed(
    authed_client: TestClient, corpus: dict[str, int], db_session: Session, second_user: User
) -> None:
    """Both subqueries read the table themselves, so the user they are
    pinned to is all that keeps one reader's list out of another's feed.
    Asked twice: once with this reader choosing nothing, where a leak would
    narrow, and once choosing ``pt``, where a leak would widen."""
    db_session.add(UserPreferenceLanguage(user_id=second_user.id, language="en"))
    db_session.commit()

    assert _visible(authed_client) == set(CORPUS)

    _save(authed_client, languages=["pt"])
    assert _visible(authed_client) == {"portuguese", "undetermined"}


def test_a_language_choice_does_not_reach_bookmarks(
    authed_client: TestClient, corpus: dict[str, int], db_session: Session, test_user: User
) -> None:
    """The exemption ``mute_predicates`` records, for the same reason."""
    db_session.add(Bookmark(user_id=test_user.id, feed_item_id=corpus["thai"]))
    db_session.commit()
    _save(authed_client, languages=["en"])

    bookmarks = authed_client.get("/api/v1/bookmarks").json()

    assert [entry["item"]["title"] for entry in bookmarks["bookmarks"]] == ["thai"]


def test_the_card_carries_its_language(authed_client: TestClient, corpus: dict[str, int]) -> None:
    response = authed_client.get("/api/v1/feed", params={"limit": 100, "sources": ["dev-to"]})

    languages = {item["title"]: item["language"] for item in response.json()["items"]}
    assert languages == CORPUS


def test_the_predicate_costs_no_statement(
    authed_client: TestClient, corpus: dict[str, int], engine: Engine
) -> None:
    """Chosen languages are read inside the feed's statement, so choosing
    some must not add a query to the page."""
    with count_statements(engine) as before:
        authed_client.get("/api/v1/feed", params={"sources": ["dev-to"]})
    _save(authed_client, languages=["en", "pt"])
    with count_statements(engine) as after:
        authed_client.get("/api/v1/feed", params={"sources": ["dev-to"]})

    assert len(after) == len(before)


# --- the preference -----------------------------------------------------


def test_codes_are_folded_deduplicated_and_sorted(
    authed_client: TestClient, db_session: Session, test_user: User
) -> None:
    saved = _save(authed_client, languages=["PT", " en ", "pt"])

    assert saved["languages"] == ["en", "pt"]
    stored = db_session.scalars(
        select(UserPreferenceLanguage.language).where(
            UserPreferenceLanguage.user_id == test_user.id
        )
    ).all()
    assert sorted(stored) == ["en", "pt"]


@pytest.mark.parametrize("code", ["english", "xx", "pt-BR"])
def test_a_code_the_detector_never_emits_is_refused(authed_client: TestClient, code: str) -> None:
    """Stored, a code that matches nothing would hide every detected item.
    ``pt-BR`` is here because it fits the column and reads as reasonable,
    and the detector does not emit regions."""
    _save(authed_client, languages=["en"])

    response = authed_client.patch("/api/v1/me/preferences", json={"languages": [code, "en"]})

    assert response.status_code == 422
    # Refused whole: the earlier list is untouched.
    assert authed_client.get("/api/v1/me").json()["preferences"]["languages"] == ["en"]


def test_an_absent_field_leaves_the_list_alone(authed_client: TestClient) -> None:
    _save(authed_client, languages=["en"])

    saved = _save(authed_client, theme="dark")

    assert saved["languages"] == ["en"]


def test_the_list_is_bounded(authed_client: TestClient) -> None:
    response = authed_client.patch(
        "/api/v1/me/preferences", json={"languages": ["en"] * (MAX_LANGUAGES + 1)}
    )

    assert response.status_code == 422
