"""Topic links written by ingest, and which writer owns each one.

The contract ``feed_item_topics.origin`` exists to keep: the ruleset may
delete and reinsert its own links, and must never be able to delete the
operator's.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FeedItem, FeedItemTopic, Source, Topic, TopicOrigin
from app.ingest.normalise import NormalisedItem
from app.ingest.store import upsert_items

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

#: A section the ruleset maps, and one it does not.
SPORT_URL = "https://www.bbc.co.uk/sport/cricket/videos/cq70dnxrg79lo"
NEWS_URL = "https://www.bbc.co.uk/news/articles/cx980qj57r89o"


def item(url: str) -> NormalisedItem:
    return NormalisedItem(
        canonical_url=url, title="Title", summary=None, published_at=NOW, image_url=None
    )


def links(session: Session, url: str) -> dict[str, TopicOrigin]:
    """Topic slug to origin, for the item at *url*."""
    rows = session.execute(
        select(Topic.slug, FeedItemTopic.origin)
        .join(FeedItemTopic, FeedItemTopic.topic_id == Topic.id)
        .join(FeedItem, FeedItem.id == FeedItemTopic.feed_item_id)
        .where(FeedItem.canonical_url == url)
    ).all()
    return dict(rows)  # type: ignore[arg-type]


def add_topic(session: Session, slug: str) -> Topic:
    row = Topic(slug=slug, name=slug.title())
    session.add(row)
    session.flush()
    return row


def test_a_section_in_the_url_adds_a_topic_beside_the_sources(
    db_session: Session, source: Source, topic: Topic
) -> None:
    sport = add_topic(db_session, "sport")
    source_topic = topic

    upsert_items(
        db_session,
        source_id=source.id,
        items=[item(SPORT_URL)],
        topic_ids=[source_topic.id],
        fetched_at=NOW,
    )

    assert links(db_session, SPORT_URL) == {
        source_topic.slug: TopicOrigin.SOURCE,
        sport.slug: TopicOrigin.RULE,
    }


def test_an_item_with_no_section_keeps_only_its_sources_topics(
    db_session: Session, source: Source, topic: Topic
) -> None:
    add_topic(db_session, "sport")
    source_topic = topic

    upsert_items(
        db_session,
        source_id=source.id,
        items=[item(NEWS_URL)],
        topic_ids=[source_topic.id],
        fetched_at=NOW,
    )

    assert links(db_session, NEWS_URL) == {source_topic.slug: TopicOrigin.SOURCE}


def test_a_rule_slug_the_instance_has_no_topic_for_is_skipped(
    db_session: Session, source: Source, topic: Topic
) -> None:
    """Nothing seeds ``sport`` here. The refresh must still store the item
    and the source's own links rather than failing on a missing row."""
    source_topic = topic

    inserted = upsert_items(
        db_session,
        source_id=source.id,
        items=[item(SPORT_URL)],
        topic_ids=[source_topic.id],
        fetched_at=NOW,
    )

    assert inserted == 1
    assert links(db_session, SPORT_URL) == {source_topic.slug: TopicOrigin.SOURCE}


def test_the_source_wins_where_both_name_the_same_topic(
    db_session: Session, source: Source
) -> None:
    """A rule derives ``sport`` on the first refresh. The operator then
    adds ``sport`` to the source. The link has to become the operator's,
    or the next re-tag would delete something they asserted."""
    sport = add_topic(db_session, "sport")

    upsert_items(
        db_session, source_id=source.id, items=[item(SPORT_URL)], topic_ids=[], fetched_at=NOW
    )
    assert links(db_session, SPORT_URL) == {"sport": TopicOrigin.RULE}

    upsert_items(
        db_session,
        source_id=source.id,
        items=[item(SPORT_URL)],
        topic_ids=[sport.id],
        fetched_at=NOW,
    )
    assert links(db_session, SPORT_URL) == {"sport": TopicOrigin.SOURCE}


def test_the_promotion_is_one_way(db_session: Session, source: Source) -> None:
    """Once the operator has said it, a later refresh that also derives it
    from the URL must not hand it back to the ruleset."""
    sport = add_topic(db_session, "sport")

    upsert_items(
        db_session,
        source_id=source.id,
        items=[item(SPORT_URL)],
        topic_ids=[sport.id],
        fetched_at=NOW,
    )
    upsert_items(
        db_session, source_id=source.id, items=[item(SPORT_URL)], topic_ids=[], fetched_at=NOW
    )

    assert links(db_session, SPORT_URL) == {"sport": TopicOrigin.SOURCE}


def test_re_ingesting_the_same_batch_does_not_duplicate_rule_links(
    db_session: Session, source: Source
) -> None:
    add_topic(db_session, "sport")
    for _ in range(3):
        upsert_items(
            db_session, source_id=source.id, items=[item(SPORT_URL)], topic_ids=[], fetched_at=NOW
        )

    rows = db_session.scalars(select(FeedItemTopic.topic_id)).all()
    assert len(rows) == 1
