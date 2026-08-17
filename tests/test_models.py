"""Data-layer contract checks: lazy='raise', compound uniqueness, cascade."""

from __future__ import annotations

import pytest
from sqlalchemy import exc, func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Bookmark, FeedItem, Source, User


def _seed_item(db_session: Session) -> FeedItem:
    source = Source(
        slug="hn",
        name="Hacker News",
        feed_url="https://news.ycombinator.com/rss",
        website_url="https://news.ycombinator.com",
    )
    db_session.add(source)
    db_session.flush()
    item = FeedItem(
        source_id=source.id,
        canonical_url="https://example.org/a",
        title="A story",
        published_at=func.now(),
    )
    db_session.add(item)
    db_session.commit()
    return item


def test_implicit_lazy_load_raises(db_session: Session, test_user: User) -> None:
    user = db_session.scalars(select(User)).one()
    with pytest.raises(exc.InvalidRequestError, match="lazy='raise'"):
        _ = user.sessions


def test_explicit_eager_load_works(db_session: Session, test_user: User) -> None:
    user = db_session.scalars(select(User).options(selectinload(User.sessions))).one()
    assert user.sessions == []


def test_bookmark_compound_key_rejects_duplicates(db_session: Session, test_user: User) -> None:
    item = _seed_item(db_session)
    db_session.add(Bookmark(user_id=test_user.id, feed_item_id=item.id))
    db_session.commit()

    db_session.add(Bookmark(user_id=test_user.id, feed_item_id=item.id))
    with pytest.raises(exc.IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_deleting_user_cascades_owned_state(db_session: Session, test_user: User) -> None:
    item = _seed_item(db_session)
    db_session.add(Bookmark(user_id=test_user.id, feed_item_id=item.id))
    db_session.commit()

    db_session.delete(test_user)
    db_session.commit()
    assert db_session.scalars(select(Bookmark)).all() == []
