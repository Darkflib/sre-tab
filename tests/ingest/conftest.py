"""Fixtures for the ingest and scheduler suites.

Root fixtures stay in tests/conftest.py (Phase 0 property); everything
here is area-local (AGENTS.md).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Source, SourceTopic, Topic
from app.db.session import build_session_factory
from app.health import probes
from app.ingest.fetch import FeedFetcher, HostRateLimiter
from app.ingest.service import IngestService, SourceRef
from app.ingest.urlguard import UrlGuard
from app.settings import Settings

#: The whole suite fetches this one host, resolving to this one address.
#: The fetcher pins to the address, so respx routes are registered
#: against PINNED_URL rather than FEED_URL.
TEST_HOST = "feeds.example.com"
TEST_IP = "93.184.216.34"
FEED_URL = f"https://{TEST_HOST}/rss"
PINNED_URL = f"https://{TEST_IP}/rss"

#: A second host, for redirect-chain tests.
OTHER_HOST = "cdn.example.net"
OTHER_IP = "198.51.44.10"

RESOLVER_TABLE: dict[str, Sequence[str]] = {
    TEST_HOST: [TEST_IP],
    OTHER_HOST: [OTHER_IP],
    "private.example.com": ["10.0.0.7"],
    "metadata-host.example.com": ["169.254.169.254"],
}


class StubResolver:
    def __init__(self, table: dict[str, Sequence[str]] | None = None) -> None:
        self.table = dict(RESOLVER_TABLE if table is None else table)
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> Sequence[str]:
        self.calls.append((host, port))
        try:
            return self.table[host]
        except KeyError as exc:  # pragma: no cover - a test asked for an unknown host
            raise AssertionError(f"unexpected DNS lookup for {host}") from exc


@pytest.fixture(autouse=True)
def _isolate_probe_registry() -> Iterator[None]:
    """The probe registry is process-wide; do not leak into other suites."""
    liveness = dict(probes._liveness)
    readiness = dict(probes._readiness)
    yield
    probes._liveness = liveness
    probes._readiness = readiness


@pytest.fixture
def ingest_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="sqlite://",
        session_secret=SecretStr("test-session-secret"),
        source_refresh_enabled=False,
        source_fetch_timeout_seconds=5.0,
        source_fetch_max_bytes=64_000,
        source_fetch_max_redirects=3,
        feed_retention_days=90,
    )


@pytest.fixture
def resolver() -> StubResolver:
    return StubResolver()


@pytest.fixture
def guard(resolver: StubResolver) -> UrlGuard:
    return UrlGuard(resolver=resolver)


@pytest.fixture
def fetcher(ingest_settings: Settings, guard: UrlGuard) -> FeedFetcher:
    # Rate limiting is exercised on its own; elsewhere it only adds
    # wall-clock time to the suite.
    return FeedFetcher(ingest_settings, guard=guard, rate_limiter=HostRateLimiter(0.0))


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return build_session_factory(engine)


@pytest.fixture
def ingest_service(
    session_factory: sessionmaker[Session], ingest_settings: Settings, fetcher: FeedFetcher
) -> IngestService:
    return IngestService(session_factory, ingest_settings, fetcher=fetcher)


@pytest.fixture
def topic(db_session: Session) -> Topic:
    row = Topic(slug="tech-industry", name="Tech industry")
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def source(db_session: Session, topic: Topic) -> Source:
    row = Source(
        slug="example",
        name="Example Feed",
        feed_url=FEED_URL,
        website_url="https://feeds.example.com/",
        refresh_minutes=30,
        enabled=True,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    db_session.add(SourceTopic(source_id=row.id, topic_id=topic.id))
    db_session.commit()
    return row


@pytest.fixture
def source_ref(source: Source, topic: Topic) -> SourceRef:
    return SourceRef(
        id=source.id,
        slug=source.slug,
        feed_url=source.feed_url,
        refresh_minutes=source.refresh_minutes,
        topic_ids=(topic.id,),
    )


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


# --- feed bodies --------------------------------------------------------


def rss_body(items: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>Example Feed</title>
    <link>https://feeds.example.com/</link>
    <description>Example</description>
{items}
  </channel>
</rss>""".encode()


RSS_ITEM_TEMPLATE = """    <item>
      <title>{title}</title>
      <link>{link}</link>
      <description>{description}</description>
      <pubDate>{pub_date}</pubDate>
      <guid>{link}</guid>
    </item>"""


@pytest.fixture
def rss_feed() -> bytes:
    return rss_body(
        "\n".join(
            [
                RSS_ITEM_TEMPLATE.format(
                    title="First article",
                    link="https://example.org/first",
                    description="&lt;p&gt;Plain &lt;b&gt;enough&lt;/b&gt;&lt;/p&gt;",
                    pub_date="Mon, 17 Aug 2026 09:00:00 GMT",
                ),
                RSS_ITEM_TEMPLATE.format(
                    title="Second article",
                    link="https://example.org/second",
                    description="Second summary",
                    pub_date="Mon, 17 Aug 2026 08:00:00 GMT",
                ),
            ]
        )
    )


@pytest.fixture
def atom_feed() -> bytes:
    return b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <id>urn:uuid:8f2b</id>
  <updated>2026-08-17T09:00:00Z</updated>
  <entry>
    <title>Atom one</title>
    <link rel="alternate" href="https://example.org/atom-one"/>
    <id>https://example.org/atom-one</id>
    <updated>2026-08-17T09:00:00Z</updated>
    <published>2026-08-17T08:30:00Z</published>
    <summary type="html">&lt;p&gt;Atom &lt;em&gt;summary&lt;/em&gt;&lt;/p&gt;</summary>
  </entry>
  <entry>
    <title>Atom two</title>
    <link rel="alternate" href="https://example.org/atom-two"/>
    <id>https://example.org/atom-two</id>
    <updated>2026-08-17T07:00:00Z</updated>
  </entry>
</feed>"""
