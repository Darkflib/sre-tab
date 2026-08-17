"""S2, end to end: seeded source -> fetch -> stored item -> filtered feed.

The failure this guards against is quiet. ``GET /feed?topics=…`` is
literal by design — it narrows to items carrying one of those topics, and
an item carrying none matches nothing. Items inherit their topics from
their source at ingest time, so a source with no ``source_topics`` rows
produces items that are invisible under every explicit topic filter while
looking perfectly healthy in the database and in the status view.

So this walks the whole path rather than asserting the pieces: seed the
catalogue with the operator CLI, refresh a source through the real ingest
service against a stubbed response, and read the result back through the
API with the filter applied.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.cli import operations as ops
from app.db.models import FeedItem, FeedItemTopic, Source, Topic
from app.db.session import build_session_factory
from app.ingest.fetch import FeedFetcher, HostRateLimiter
from app.ingest.service import IngestService
from app.ingest.urlguard import UrlGuard
from app.settings import Settings
from tests.ingest.conftest import StubResolver

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

#: Lobsters is the interesting seeded case: two default topics, so a
#: single-topic assertion would pass by accident.
SOURCE_SLUG = "lobsters"
FEED_URL = "https://lobste.rs/rss"
PINNED_URL = "https://93.184.216.34/rss"

FEED_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Lobsters</title>
    <link>https://lobste.rs/</link>
    <description>Computing</description>
    <item>
      <title>A story about init systems</title>
      <link>https://example.org/init</link>
      <description>Plain text summary</description>
      <pubDate>Mon, 17 Aug 2026 09:00:00 GMT</pubDate>
      <guid>https://example.org/init</guid>
    </item>
    <item>
      <title>A story about compilers</title>
      <link>https://example.org/compilers</link>
      <description>Another summary</description>
      <pubDate>Mon, 17 Aug 2026 08:00:00 GMT</pubDate>
      <guid>https://example.org/compilers</guid>
    </item>
  </channel>
</rss>"""


@pytest.fixture
def seeded_catalogue(db_session: Session) -> Session:
    ops.seed_catalogue(db_session)
    db_session.commit()
    return db_session


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return build_session_factory(engine)


@pytest.fixture
def ingest(session_factory: sessionmaker[Session], settings: Settings) -> IngestService:
    guard = UrlGuard(resolver=StubResolver({"lobste.rs": ["93.184.216.34"]}))
    fetcher = FeedFetcher(settings, guard=guard, rate_limiter=HostRateLimiter(0.0))
    return IngestService(session_factory, settings, fetcher=fetcher)


@respx.mock
def test_ingested_items_inherit_the_source_default_topics(
    seeded_catalogue: Session, ingest: IngestService
) -> None:
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=FEED_BODY))

    source = next(ref for ref in ingest.enabled_sources() if ref.slug == SOURCE_SLUG)
    # The seed linked two topics, and the ref carries both into the store.
    assert len(source.topic_ids) == 2

    status = ingest.refresh_source(source, now=NOW)
    assert status.consecutive_failures == 0
    assert status.last_inserted_count == 2

    seeded_catalogue.expire_all()
    rows = seeded_catalogue.execute(
        select(FeedItem.canonical_url, Topic.slug)
        .join(FeedItemTopic, FeedItemTopic.feed_item_id == FeedItem.id)
        .join(Topic, Topic.id == FeedItemTopic.topic_id)
    ).all()

    by_item: dict[str, set[str]] = {}
    for url, slug in rows:
        by_item.setdefault(url, set()).add(slug)

    assert by_item == {
        "https://example.org/init": {"open-source", "tech-industry"},
        "https://example.org/compilers": {"open-source", "tech-industry"},
    }


@respx.mock
def test_a_source_that_gains_a_topic_passes_it_to_existing_items(
    seeded_catalogue: Session, ingest: IngestService
) -> None:
    """The operator adding a topic must reach items already stored, or
    reclassifying a source would only ever affect its future."""
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=FEED_BODY))
    source = next(ref for ref in ingest.enabled_sources() if ref.slug == SOURCE_SLUG)
    ingest.refresh_source(source, now=NOW)

    ops.set_source_topics(seeded_catalogue, SOURCE_SLUG, ["open-source", "tech-industry", "devops"])
    seeded_catalogue.commit()

    refreshed = next(ref for ref in ingest.enabled_sources() if ref.slug == SOURCE_SLUG)
    ingest.refresh_source(refreshed, now=NOW)

    seeded_catalogue.expire_all()
    slugs = set(
        seeded_catalogue.scalars(
            select(Topic.slug)
            .join(FeedItemTopic, FeedItemTopic.topic_id == Topic.id)
            .join(FeedItem, FeedItem.id == FeedItemTopic.feed_item_id)
            .where(FeedItem.canonical_url == "https://example.org/init")
        )
    )
    assert slugs == {"open-source", "tech-industry", "devops"}


@respx.mock
def test_the_filtered_feed_returns_the_ingested_items(
    seeded_catalogue: Session, ingest: IngestService, authed_client: TestClient
) -> None:
    """The end of the path: an explicit ?topics= filter finds them."""
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=FEED_BODY))
    source = next(ref for ref in ingest.enabled_sources() if ref.slug == SOURCE_SLUG)
    ingest.refresh_source(source, now=NOW)

    body = authed_client.get(
        "/api/v1/feed", params={"topics": "open-source", "sources": SOURCE_SLUG}
    ).json()
    urls = {item["canonical_url"] for item in body["items"]}
    assert urls == {"https://example.org/init", "https://example.org/compilers"}
    assert all("open-source" in item["topics"] for item in body["items"])

    # A topic this source does not carry narrows to nothing, which is the
    # honest answer rather than an unfiltered page.
    empty = authed_client.get("/api/v1/feed", params={"topics": "hardware"}).json()
    assert empty["items"] == []


def test_the_seeded_catalogue_reaches_the_api(
    seeded_catalogue: Session, authed_client: TestClient
) -> None:
    body = authed_client.get("/api/v1/sources").json()
    slugs = {source["slug"] for source in body["sources"]}
    assert {"hacker-news", "lobsters", "dev-to", "lwn"} <= slugs
    assert len(body["topics"]) == 11

    lobsters = next(source for source in body["sources"] if source["slug"] == SOURCE_SLUG)
    assert lobsters["topics"] == ["open-source", "tech-industry"]
    assert lobsters["feed_url"] == FEED_URL


def test_a_new_user_gets_the_documented_default_selection(
    seeded_catalogue: Session, authed_client: TestClient
) -> None:
    """S1's real consequence: the four default slugs have to exist."""
    body = authed_client.get("/api/v1/me").json()
    assert set(body["preferences"]["sources"]) == {"hacker-news", "lobsters", "dev-to", "lwn"}
    assert len(body["preferences"]["topics"]) == 11


def test_a_disabled_source_is_not_refreshed(
    seeded_catalogue: Session, ingest: IngestService
) -> None:
    ops.set_source_enabled(seeded_catalogue, SOURCE_SLUG, enabled=False)
    seeded_catalogue.commit()
    assert all(ref.slug != SOURCE_SLUG for ref in ingest.enabled_sources())


def test_every_seeded_url_survives_the_fetch_time_allow_list(seeded_catalogue: Session) -> None:
    """The fetcher's allow-list is byte-identical to the stored feed_url,
    so a seed that stored a non-canonical form would refuse itself."""
    guard = UrlGuard(resolver=StubResolver({}))
    for feed_url in seeded_catalogue.scalars(select(Source.feed_url)):
        assert str(guard.check_static(feed_url)) == feed_url


def test_every_seeded_source_reaches_ingest_with_topics(
    seeded_catalogue: Session, ingest: IngestService
) -> None:
    """No seeded source may ingest untopiced items."""
    refs = ingest.enabled_sources()
    assert len(refs) == 7
    assert all(ref.topic_ids for ref in refs)


@respx.mock
def test_refresh_writes_status_the_cli_can_read(
    seeded_catalogue: Session, ingest: IngestService
) -> None:
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=FEED_BODY))
    source = next(ref for ref in ingest.enabled_sources() if ref.slug == SOURCE_SLUG)
    ingest.refresh_source(source, now=NOW)

    seeded_catalogue.expire_all()
    view = next(view for view in ops.refresh_status(seeded_catalogue) if view.slug == SOURCE_SLUG)
    assert view.state == "ok"
    assert view.last_success_at is not None
