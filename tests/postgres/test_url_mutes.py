"""URL mutes on PostgreSQL, against the same corpus as SQLite.

The predicate is a correlated ``EXISTS`` built from ``lower``, ``substr``,
``length``, and a ``CASE`` — functions both engines have, and none of
which they are guaranteed to agree about at the edges. ``substr`` past the
end of a string is the equality case, and it has to be ``''`` on both;
``length`` has to count what ``substr`` indexes. So the corpus and the
terms from ``tests/api/test_feed_url_mutes.py`` are run here through the
same :func:`app.services.feed.mute_predicates` the feed calls, and must
leave the same survivors — and a hundred of them must still be one query
PostgreSQL will plan and run.
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
    """Terms are stored folded, so an unfolded column would let both
    shouted rows through."""
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


def test_a_hundred_url_mutes_are_one_query_postgres_will_run(
    pg_session: Session, pg_corpus: User
) -> None:
    """The cap, on the engine that plans the correlated subquery for real."""
    terms = [f"host{n}.example/author" for n in range(99)]

    assert _surviving(pg_session, pg_corpus, *terms, "medium.com") == set(CORPUS) - {"medium"}
