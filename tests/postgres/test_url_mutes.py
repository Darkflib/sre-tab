"""URL mutes on PostgreSQL, where ``LIKE`` and ``=`` both respect case.

SQLite's ``LIKE`` ignores ASCII case, so on the development engine a
predicate that forgot to fold would still hide ``/JRandom/...`` and look
correct. PostgreSQL is where that mistake would ship, so the corpus and
the terms from ``tests/api/test_feed_url_mutes.py`` are run here through
the same :func:`app.services.feed.mute_predicates` the feed calls, and
must leave the same survivors.

The escaping is the other thing worth asking twice. SQLAlchemy's
``autoescape`` renders ``ESCAPE '/'``, and every term here is full of
``/`` — so a dialect that handled the escape character differently would
turn each ``https://`` into something else entirely.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import FeedItem, MuteKind, Source, User, UserMutedTerm
from app.services.feed import mute_predicates
from tests.api.test_feed_url_mutes import CORPUS, MUTED, SURVIVORS
from tests.postgres.conftest import pytestmark as _pytestmark

pytestmark = _pytestmark

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def pg_corpus(pg_session: Session) -> User:
    user = User(github_id=101405, github_login="darkflib")
    source = Source(
        slug="lobsters",
        name="Lobsters",
        feed_url="https://lobste.rs/rss",
        website_url="https://lobste.rs/",
    )
    pg_session.add_all([user, source])
    pg_session.flush()
    pg_session.add_all(
        FeedItem(
            source_id=source.id,
            canonical_url=url,
            title=key,
            published_at=NOW - timedelta(minutes=index),
        )
        for index, (key, url) in enumerate(CORPUS.items())
    )
    pg_session.commit()
    return user


def _surviving(session: Session, user: User, *terms: str) -> set[str]:
    session.execute(delete(UserMutedTerm).where(UserMutedTerm.user_id == user.id))
    session.add_all(UserMutedTerm(user_id=user.id, kind=MuteKind.URL, term=term) for term in terms)
    session.flush()
    statement = select(FeedItem.title)
    for predicate in mute_predicates(session, user):
        statement = statement.where(predicate)
    return set(session.scalars(statement))


def test_nothing_muted_hides_nothing_on_postgres(pg_session: Session, pg_corpus: User) -> None:
    assert _surviving(pg_session, pg_corpus) == set(CORPUS)


def test_url_mutes_leave_the_same_survivors_on_postgres(
    pg_session: Session, pg_corpus: User
) -> None:
    assert _surviving(pg_session, pg_corpus, *MUTED) == SURVIVORS


def test_an_author_mute_is_case_blind_on_postgres(pg_session: Session, pg_corpus: User) -> None:
    """The divergence this file exists for: without ``lower`` on the
    column, both shouted rows survive here while SQLite hides one of them."""
    hidden = set(CORPUS) - _surviving(pg_session, pg_corpus, "dev.to/jrandom")

    assert {"author-shouted", "author-root-shouted"} <= hidden
    assert "author-neighbour" not in hidden


@pytest.mark.parametrize(
    ("term", "hidden"),
    [("pct.example/100%", {"percent"}), ("under.example/snake_case", {"underscore"})],
)
def test_a_like_metacharacter_is_a_literal_on_postgres(
    pg_session: Session, pg_corpus: User, term: str, hidden: set[str]
) -> None:
    assert set(CORPUS) - _surviving(pg_session, pg_corpus, term) == hidden
