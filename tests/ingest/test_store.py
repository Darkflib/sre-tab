"""Dedup, idempotent re-ingest, and retention pruning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Bookmark, FeedItem, FeedItemTopic, Source, Topic, User
from app.ingest.normalise import NormalisedItem
from app.ingest.store import prune_feed_items, upsert_items

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def item(url: str, *, title: str = "Title", published: datetime | None = None) -> NormalisedItem:
    return NormalisedItem(
        canonical_url=url,
        title=title,
        summary="Summary",
        published_at=published or NOW,
        image_url=None,
    )


def count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(FeedItem)) or 0


# --- dedup --------------------------------------------------------------


def test_repeated_ingest_does_not_duplicate(db_session: Session, source: Source) -> None:
    items = [item("https://example.org/a"), item("https://example.org/b")]

    assert (
        upsert_items(db_session, source_id=source.id, items=items, topic_ids=[], fetched_at=NOW)
        == 2
    )
    assert count(db_session) == 2

    # Three more refreshes of the same feed.
    for _ in range(3):
        assert (
            upsert_items(db_session, source_id=source.id, items=items, topic_ids=[], fetched_at=NOW)
            == 0
        )
    assert count(db_session) == 2


def test_existing_items_are_not_reordered_or_resurrected(
    db_session: Session, source: Source
) -> None:
    """Criterion 2: a re-fetch must not move an item back to the top."""
    original = item("https://example.org/a", title="Original", published=NOW - timedelta(days=2))
    upsert_items(db_session, source_id=source.id, items=[original], topic_ids=[], fetched_at=NOW)
    stored = db_session.scalars(select(FeedItem)).one()
    original_published, original_fetched = stored.published_at, stored.fetched_at

    later = NOW + timedelta(hours=6)
    restated = item("https://example.org/a", title="Rewritten headline", published=later)
    upsert_items(db_session, source_id=source.id, items=[restated], topic_ids=[], fetched_at=later)

    db_session.expire_all()
    stored = db_session.scalars(select(FeedItem)).one()
    assert stored.title == "Original"
    assert stored.published_at == original_published
    assert stored.fetched_at == original_fetched


def test_a_new_item_is_added_alongside_existing_ones(db_session: Session, source: Source) -> None:
    upsert_items(
        db_session,
        source_id=source.id,
        items=[item("https://example.org/a")],
        topic_ids=[],
        fetched_at=NOW,
    )
    inserted = upsert_items(
        db_session,
        source_id=source.id,
        items=[item("https://example.org/a"), item("https://example.org/b")],
        topic_ids=[],
        fetched_at=NOW,
    )
    assert inserted == 1
    assert count(db_session) == 2


def test_topic_links_are_idempotent(db_session: Session, source: Source, topic: Topic) -> None:
    items = [item("https://example.org/a")]
    for _ in range(3):
        upsert_items(
            db_session, source_id=source.id, items=items, topic_ids=[topic.id], fetched_at=NOW
        )
    links = db_session.scalars(select(FeedItemTopic.topic_id)).all()
    assert list(links) == [topic.id]


def test_topics_added_later_reach_existing_items(
    db_session: Session, source: Source, topic: Topic
) -> None:
    items = [item("https://example.org/a")]
    upsert_items(db_session, source_id=source.id, items=items, topic_ids=[], fetched_at=NOW)
    upsert_items(db_session, source_id=source.id, items=items, topic_ids=[topic.id], fetched_at=NOW)
    assert db_session.scalars(select(FeedItemTopic.topic_id)).all() == [topic.id]


def test_empty_batch_is_a_no_op(db_session: Session, source: Source) -> None:
    assert (
        upsert_items(db_session, source_id=source.id, items=[], topic_ids=[], fetched_at=NOW) == 0
    )


# --- prune --------------------------------------------------------------


def test_prune_honours_the_retention_window(db_session: Session, source: Source) -> None:
    cutoff = NOW - timedelta(days=90)
    upsert_items(
        db_session,
        source_id=source.id,
        items=[
            item("https://example.org/ancient", published=cutoff - timedelta(days=1)),
            item("https://example.org/edge", published=cutoff),
            item("https://example.org/fresh", published=NOW - timedelta(days=1)),
        ],
        topic_ids=[],
        fetched_at=NOW,
    )

    assert prune_feed_items(db_session, cutoff=cutoff) == 1
    remaining = set(db_session.scalars(select(FeedItem.canonical_url)))
    # The boundary is exclusive: an item published exactly at the cutoff
    # is still inside the window.
    assert remaining == {"https://example.org/edge", "https://example.org/fresh"}


def test_prune_with_nothing_to_do_removes_nothing(db_session: Session, source: Source) -> None:
    upsert_items(
        db_session,
        source_id=source.id,
        items=[item("https://example.org/a")],
        topic_ids=[],
        fetched_at=NOW,
    )
    assert prune_feed_items(db_session, cutoff=NOW - timedelta(days=90)) == 0
    assert count(db_session) == 1


def test_prune_cascades_to_dependent_rows(db_session: Session, source: Source) -> None:
    user = User(github_id=99, github_login="pruner")
    db_session.add(user)
    db_session.commit()

    cutoff = NOW - timedelta(days=90)
    upsert_items(
        db_session,
        source_id=source.id,
        items=[item("https://example.org/old", published=cutoff - timedelta(days=5))],
        topic_ids=[],
        fetched_at=NOW,
    )
    stored = db_session.scalars(select(FeedItem)).one()
    db_session.add(Bookmark(user_id=user.id, feed_item_id=stored.id))
    db_session.commit()

    assert prune_feed_items(db_session, cutoff=cutoff) == 1
    assert db_session.scalars(select(Bookmark)).all() == []
