"""End-to-end refresh, and the per-source isolation that must hold."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
import time_machine
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import FeedItem, Source, SourceTopic, Topic
from app.ingest.service import IngestService, SourceRef
from tests.ingest.conftest import FEED_URL, OTHER_HOST, OTHER_IP, PINNED_URL

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def stored_urls(session: Session) -> set[str]:
    return set(session.scalars(select(FeedItem.canonical_url)))


# --- happy path ---------------------------------------------------------


@respx.mock
def test_refresh_stores_normalised_items(
    ingest_service: IngestService, source_ref: SourceRef, db_session: Session, rss_feed: bytes
) -> None:
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=rss_feed))

    status = ingest_service.refresh_source(source_ref, now=NOW)

    assert status.consecutive_failures == 0
    assert status.last_inserted_count == 2
    assert stored_urls(db_session) == {
        "https://example.org/first",
        "https://example.org/second",
    }
    item = db_session.scalars(
        select(FeedItem).where(FeedItem.canonical_url == "https://example.org/first")
    ).one()
    # Feed HTML never reaches the column.
    assert item.summary == "Plain enough"
    assert "<" not in (item.summary or "")


@respx.mock
def test_atom_refresh_stores_items(
    ingest_service: IngestService, source_ref: SourceRef, db_session: Session, atom_feed: bytes
) -> None:
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=atom_feed))
    ingest_service.refresh_source(source_ref, now=NOW)
    assert stored_urls(db_session) == {
        "https://example.org/atom-one",
        "https://example.org/atom-two",
    }


@respx.mock
def test_refresh_links_source_topics(
    ingest_service: IngestService, source_ref: SourceRef, db_session: Session, rss_feed: bytes
) -> None:
    from app.db.models import FeedItemTopic

    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=rss_feed))
    ingest_service.refresh_source(source_ref, now=NOW)
    assert db_session.scalar(select(func.count()).select_from(FeedItemTopic)) == 2


# --- criterion 2 --------------------------------------------------------


@respx.mock
def test_repeated_refresh_does_not_duplicate(
    ingest_service: IngestService, source_ref: SourceRef, db_session: Session, rss_feed: bytes
) -> None:
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=rss_feed))

    for _ in range(4):
        ingest_service.refresh_source(source_ref, now=NOW)

    assert db_session.scalar(select(func.count()).select_from(FeedItem)) == 2
    assert ingest_service.status.get(source_ref.id) is not None
    assert ingest_service.status.get(source_ref.id).last_inserted_count == 0  # type: ignore[union-attr]


# --- per-source isolation -----------------------------------------------


@pytest.fixture
def two_sources(db_session: Session, topic: Topic) -> tuple[SourceRef, SourceRef]:
    broken = Source(
        slug="broken",
        name="Broken Feed",
        feed_url=f"https://{OTHER_HOST}/rss",
        website_url=f"https://{OTHER_HOST}/",
        refresh_minutes=30,
    )
    working = Source(
        slug="working",
        name="Working Feed",
        feed_url=FEED_URL,
        website_url="https://feeds.example.com/",
        refresh_minutes=30,
    )
    db_session.add_all([broken, working])
    db_session.commit()
    db_session.add_all(
        [
            SourceTopic(source_id=broken.id, topic_id=topic.id),
            SourceTopic(source_id=working.id, topic_id=topic.id),
        ]
    )
    db_session.commit()
    return (
        SourceRef(broken.id, broken.slug, broken.feed_url, 30, (topic.id,)),
        SourceRef(working.id, working.slug, working.feed_url, 30, (topic.id,)),
    )


@respx.mock
@pytest.mark.parametrize(
    ("label", "response"),
    [
        ("network-error", httpx.ConnectError("no route")),
        ("timeout", httpx.ReadTimeout("slow")),
        ("http-500", None),
        ("malformed-xml", None),
        ("oversized", None),
        ("xxe", None),
    ],
)
def test_one_source_failing_leaves_the_other_untouched(
    label: str,
    response: object,
    ingest_service: IngestService,
    two_sources: tuple[SourceRef, SourceRef],
    db_session: Session,
    rss_feed: bytes,
) -> None:
    broken, working = two_sources
    broken_url = f"https://{OTHER_IP}/rss"

    if isinstance(response, Exception):
        respx.get(broken_url).mock(side_effect=response)
    elif label == "http-500":
        respx.get(broken_url).mock(return_value=httpx.Response(500))
    elif label == "malformed-xml":
        respx.get(broken_url).mock(return_value=httpx.Response(200, content=b"<rss><broken"))
    elif label == "oversized":
        respx.get(broken_url).mock(return_value=httpx.Response(200, content=b"x" * 100_000))
    else:
        respx.get(broken_url).mock(
            return_value=httpx.Response(
                200,
                content=b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                b"<rss version='2.0'><channel><title>&x;</title></channel></rss>",
            )
        )
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=rss_feed))

    statuses = ingest_service.refresh_all([broken, working])

    assert statuses[0].consecutive_failures == 1
    assert statuses[0].last_error_class is not None
    assert statuses[1].consecutive_failures == 0
    assert stored_urls(db_session) == {
        "https://example.org/first",
        "https://example.org/second",
    }


@respx.mock
def test_a_failing_refresh_never_deletes_stored_items(
    ingest_service: IngestService, source_ref: SourceRef, db_session: Session, rss_feed: bytes
) -> None:
    route = respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=rss_feed))
    ingest_service.refresh_source(source_ref, now=NOW)
    before = stored_urls(db_session)

    route.mock(side_effect=httpx.ConnectError("down"))
    for _ in range(3):
        ingest_service.refresh_source(source_ref, now=NOW)

    assert stored_urls(db_session) == before
    status = ingest_service.status.get(source_ref.id)
    assert status is not None
    assert status.consecutive_failures == 3
    assert status.last_success_at is not None


@respx.mock
def test_refresh_source_never_raises(ingest_service: IngestService, source_ref: SourceRef) -> None:
    respx.get(PINNED_URL).mock(side_effect=RuntimeError("something unexpected"))
    status = ingest_service.refresh_source(source_ref, now=NOW)
    assert status.consecutive_failures == 1


@respx.mock
def test_an_unfetchable_configured_url_is_recorded_not_raised(
    ingest_service: IngestService, db_session: Session, topic: Topic
) -> None:
    """A source configured with a hostile URL fails only itself."""
    bad = Source(
        slug="bad",
        name="Bad",
        feed_url="https://169.254.169.254/latest/meta-data/",
        website_url="https://example.com/",
        refresh_minutes=30,
    )
    db_session.add(bad)
    db_session.commit()
    ref = SourceRef(bad.id, bad.slug, bad.feed_url, 30, ())

    status = ingest_service.refresh_source(ref, now=NOW)

    assert status.last_error_class == "UnsafeTargetError"
    assert len(respx.calls) == 0


# --- scheduling metadata ------------------------------------------------


@respx.mock
def test_success_sets_the_next_due_time_from_refresh_minutes(
    ingest_service: IngestService, source_ref: SourceRef, rss_feed: bytes
) -> None:
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=rss_feed))
    status = ingest_service.refresh_source(source_ref, now=NOW)
    assert status.next_due_at == NOW + timedelta(minutes=source_ref.refresh_minutes)
    assert not ingest_service.status.is_due(source_ref.id, now=NOW + timedelta(minutes=5))
    assert ingest_service.status.is_due(source_ref.id, now=NOW + timedelta(minutes=31))


@respx.mock
def test_repeated_failure_backs_off(ingest_service: IngestService, source_ref: SourceRef) -> None:
    respx.get(PINNED_URL).mock(side_effect=httpx.ConnectError("down"))
    first = ingest_service.refresh_source(source_ref, now=NOW)
    second = ingest_service.refresh_source(source_ref, now=NOW)
    third = ingest_service.refresh_source(source_ref, now=NOW)
    assert first.next_due_at is not None
    assert second.next_due_at is not None
    assert third.next_due_at is not None
    assert first.next_due_at < second.next_due_at < third.next_due_at


def test_disabled_sources_are_not_refreshed(
    ingest_service: IngestService, db_session: Session, source: Source
) -> None:
    source.enabled = False
    db_session.commit()
    assert ingest_service.enabled_sources() == []


@time_machine.travel(NOW, tick=False)
def test_a_never_fetched_source_is_due_immediately(
    ingest_service: IngestService, source: Source
) -> None:
    assert [ref.id for ref in ingest_service.due_sources()] == [source.id]


# --- prune --------------------------------------------------------------


@respx.mock
@time_machine.travel(NOW, tick=False)
def test_prune_uses_the_configured_retention_window(
    ingest_service: IngestService, source_ref: SourceRef, db_session: Session
) -> None:
    from app.ingest.normalise import NormalisedItem
    from app.ingest.store import upsert_items

    cutoff = NOW - timedelta(days=90)
    upsert_items(
        db_session,
        source_id=source_ref.id,
        items=[
            NormalisedItem(
                canonical_url="https://example.org/old",
                title="Old",
                summary=None,
                published_at=cutoff - timedelta(days=1),
                image_url=None,
            ),
            NormalisedItem(
                canonical_url="https://example.org/new",
                title="New",
                summary=None,
                published_at=NOW,
                image_url=None,
            ),
        ],
        topic_ids=[],
        fetched_at=NOW,
    )

    assert ingest_service.prune() == 1
    assert stored_urls(db_session) == {"https://example.org/new"}


@respx.mock
def test_items_older_than_retention_are_never_stored(
    ingest_service: IngestService, source_ref: SourceRef, db_session: Session
) -> None:
    """Otherwise every refresh re-inserts what prune just deleted."""
    from tests.ingest.conftest import RSS_ITEM_TEMPLATE, rss_body

    body = rss_body(
        RSS_ITEM_TEMPLATE.format(
            title="Ancient",
            link="https://example.org/ancient",
            description="old",
            pub_date="Tue, 01 Jan 2019 00:00:00 GMT",
        )
    )
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=body))
    ingest_service.refresh_source(source_ref, now=NOW)
    assert stored_urls(db_session) == set()
