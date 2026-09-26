"""Language narrowing on PostgreSQL, against the same corpus as SQLite.

The predicate is an ``OR`` of ``IS NULL``, an uncorrelated ``NOT EXISTS``,
and an ``IN`` over a subquery — plain SQL on both engines, but the
``NULL`` branch is exactly where three-valued logic differs from what a
reader expects, so :data:`CORPUS` from ``tests/api/test_feed_languages.py``
is run through :func:`app.services.feed.language_predicate` here too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import FeedItem, Source, User, UserPreferenceLanguage
from app.services.feed import language_predicate
from tests.api.test_feed_languages import CORPUS
from tests.postgres.conftest import pytestmark as _pytestmark

pytestmark = _pytestmark

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def pg_corpus(pg_session: Session) -> User:
    user = User(github_id=101405, github_login="darkflib")
    other = User(github_id=1, github_login="someone-else")
    source = Source(
        slug="dev-to", name="DEV", feed_url="https://dev.to/feed", website_url="https://dev.to/"
    )
    pg_session.add_all([user, other, source])
    pg_session.flush()
    pg_session.add_all(
        FeedItem(
            source_id=source.id,
            canonical_url=f"https://dev.to/someone/{title}",
            title=title,
            language=language,
            published_at=NOW - timedelta(minutes=index),
        )
        for index, (title, language) in enumerate(CORPUS.items())
    )
    # Another reader's choice, which must reach nobody else's feed.
    pg_session.add(UserPreferenceLanguage(user_id=other.id, language="th"))
    pg_session.commit()
    return user


def _visible(session: Session, user: User, *languages: str) -> set[str]:
    session.execute(delete(UserPreferenceLanguage).where(UserPreferenceLanguage.user_id == user.id))
    session.add_all(UserPreferenceLanguage(user_id=user.id, language=code) for code in languages)
    session.flush()
    return set(session.scalars(select(FeedItem.title).where(language_predicate(user))))


def test_no_choice_narrows_nothing_on_postgres(pg_session: Session, pg_corpus: User) -> None:
    assert _visible(pg_session, pg_corpus) == set(CORPUS)


def test_english_keeps_the_undetermined_on_postgres(pg_session: Session, pg_corpus: User) -> None:
    assert _visible(pg_session, pg_corpus, "en") == {"english", "english-too", "undetermined"}


def test_a_union_on_postgres(pg_session: Session, pg_corpus: User) -> None:
    assert _visible(pg_session, pg_corpus, "pt", "es") == {"portuguese", "spanish", "undetermined"}
