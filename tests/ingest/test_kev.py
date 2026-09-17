"""The CISA KEV adapter: the parse, its bounds, and how a source reaches it."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.cli.catalogue import SOURCES
from app.db.models import FeedItem, FeedItemTopic, Source, SourceTopic, Topic
from app.ingest import kev
from app.ingest.errors import DocumentTooComplexError, ParseError
from app.ingest.fetch import FeedFetcher, HostRateLimiter
from app.ingest.kev import CISA_KEV_FEED_URL, MAX_NODES, parse_kev_catalogue
from app.ingest.service import ADAPTERS, IngestService, SourceRef
from app.ingest.urlguard import UrlGuard
from app.settings import Settings
from tests.ingest.conftest import PINNED_URL, TEST_IP, StubResolver

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
RELEASED = "2026-09-16T18:47:50.6796Z"
PAGE = "https://www.cisa.gov/known-exploited-vulnerabilities-catalog?search_api_fulltext="
KEV_PINNED_URL = f"https://{TEST_IP}/sites/default/files/feeds/known_exploited_vulnerabilities.json"


def vulnerability(cve_id: str, date_added: str, **overrides: Any) -> dict[str, Any]:
    """One entry, shaped as the live catalogue shapes them."""
    entry: dict[str, Any] = {
        "cveID": cve_id,
        "vendorProject": "Example",
        "product": "Widget",
        "vulnerabilityName": "Example Widget Path Traversal Vulnerability",
        "dateAdded": date_added,
        "shortDescription": "Example Widget contains a path traversal vulnerability.",
        "requiredAction": "Apply mitigations per vendor instructions.",
        "dueDate": "2026-10-07",
        "knownRansomwareCampaignUse": "Unknown",
        "forensicTriage": "No",
        "notes": f"https://nvd.nist.gov/vuln/detail/{cve_id}",
        "cwes": ["CWE-22"],
    }
    entry.update(overrides)
    return entry


def catalogue(*entries: Any, released: str | None = RELEASED) -> bytes:
    document: dict[str, Any] = {
        "title": "CISA Catalog of Known Exploited Vulnerabilities",
        "catalogVersion": "2026.09.16",
        "count": len(entries),
        "vulnerabilities": list(entries),
    }
    if released is not None:
        document["dateReleased"] = released
    return json.dumps(document).encode()


# --- the parse ------------------------------------------------------------


def test_each_vulnerability_becomes_an_entry() -> None:
    parsed = parse_kev_catalogue(
        catalogue(
            vulnerability(
                "CVE-2026-58704",
                "2026-09-16",
                vulnerabilityName="Google Pixel Improper Authorization Vulnerability",
                knownRansomwareCampaignUse="Known",
            )
        )
    )

    assert parsed.version == "cisa-kev"
    assert parsed.title == "CISA Catalog of Known Exploited Vulnerabilities"
    assert parsed.image_url is None
    (entry,) = parsed.entries
    assert entry.title == "CVE-2026-58704: Google Pixel Improper Authorization Vulnerability"
    assert entry.link == f"{PAGE}CVE-2026-58704"
    assert entry.entry_id == "CVE-2026-58704"
    assert entry.summary == (
        "Example Widget contains a path traversal vulnerability. "
        "Known to be used in ransomware campaigns. "
        "Federal remediation due 2026-10-07."
    )


def test_the_summary_omits_what_the_entry_does_not_say() -> None:
    (entry,) = parse_kev_catalogue(
        catalogue(
            vulnerability(
                "CVE-2026-0001",
                "2026-09-16",
                shortDescription="No full stop here",
                knownRansomwareCampaignUse="Unknown",
                dueDate="soon",
            )
        )
    ).entries
    assert entry.summary == "No full stop here."


def test_an_entry_added_on_release_day_takes_the_release_time() -> None:
    """Midnight would file this evening's additions beneath the whole day's
    news, which is not where anyone looks for something new."""
    newer, older = parse_kev_catalogue(
        catalogue(
            vulnerability("CVE-2026-0002", "2026-09-16"),
            vulnerability("CVE-2026-0001", "2026-09-14"),
        )
    ).entries
    assert newer.published == datetime(2026, 9, 16, 18, 47, 50, 679600, tzinfo=UTC)
    assert older.published == datetime(2026, 9, 14, tzinfo=UTC)


def test_without_a_release_time_every_entry_takes_midnight() -> None:
    (entry,) = parse_kev_catalogue(
        catalogue(vulnerability("CVE-2026-0002", "2026-09-16"), released=None)
    ).entries
    assert entry.published == datetime(2026, 9, 16, tzinfo=UTC)


def test_entries_are_kept_newest_first_whatever_order_the_document_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap sits far below the catalogue's size. Trusting document order
    would keep only the oldest entries if CISA ever reversed it — the very
    ones retention is about to throw away."""
    monkeypatch.setattr(kev, "MAX_ENTRIES", 2)
    parsed = parse_kev_catalogue(
        catalogue(
            vulnerability("CVE-2021-0001", "2021-11-03"),
            vulnerability("CVE-2024-0001", "2024-05-01"),
            vulnerability("CVE-2026-0001", "2026-09-16"),
            vulnerability("CVE-2025-0001", "2025-01-01"),
        )
    )
    assert [entry.entry_id for entry in parsed.entries] == ["CVE-2026-0001", "CVE-2025-0001"]


@pytest.mark.parametrize(
    "malformed",
    [
        vulnerability("not-a-cve", "2026-09-16"),
        vulnerability("CVE-2026-0003&x=1", "2026-09-16"),
        vulnerability("", "2026-09-16"),
        vulnerability("CVE-2026-0004", "16/09/2026"),
        vulnerability("CVE-2026-0005", "2026-02-30"),
        {"cveID": "CVE-2026-0006"},
        {"cveID": 20260007, "dateAdded": "2026-09-16"},
        "CVE-2026-0008",
        None,
    ],
    ids=[
        "bad-id",
        "id-carrying-a-query",
        "empty-id",
        "bad-date",
        "impossible-date",
        "no-date",
        "numeric-id",
        "bare-string",
        "null",
    ],
)
def test_one_malformed_entry_does_not_cost_the_rest(malformed: Any) -> None:
    parsed = parse_kev_catalogue(catalogue(malformed, vulnerability("CVE-2026-0009", "2026-09-16")))
    assert [entry.entry_id for entry in parsed.entries] == ["CVE-2026-0009"]


def test_a_bom_is_tolerated() -> None:
    body = b"\xef\xbb\xbf" + catalogue(vulnerability("CVE-2026-0010", "2026-09-16"))
    assert len(parse_kev_catalogue(body).entries) == 1


# --- refusals -------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"<rss version='2.0'><channel/></rss>",
        b'{"vulnerabilities": [',
        b"\xff\xfe\xfa not text",
        b"[]",
        b'{"items": []}',
        b'{"vulnerabilities": {"CVE-2026-0001": {}}}',
        # A usable entry, so the digit limit is the only thing that can
        # refuse it rather than the empty-catalogue check.
        catalogue(vulnerability("CVE-2026-0014", "2026-09-16"))[:-1]
        + b', "extra": '
        + b"9" * 5000
        + b"}",
    ],
    ids=[
        "empty",
        "rss",
        "truncated",
        "undecodable",
        "top-level-list",
        "json-feed-shaped",
        "vulnerabilities-not-a-list",
        "integer-past-the-digit-limit",
    ],
)
def test_anything_but_a_kev_catalogue_is_a_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_kev_catalogue(body)


def test_a_document_over_the_node_ceiling_is_refused_before_it_is_parsed() -> None:
    """Unparseable as well as oversized, so only the gate can be what
    refuses it: were ``json.loads`` reached, this would be a plain
    ``ParseError`` about the missing close bracket."""
    body = b'{"vulnerabilities": [' + b"{}," * (MAX_NODES // 2)
    with pytest.raises(DocumentTooComplexError):
        parse_kev_catalogue(body)


def _padded_to(nodes: int) -> bytes:
    """A well-formed catalogue holding one usable entry, padded with bare
    numbers — which the adapter skips — to exactly *nodes*."""
    head = (
        b'{"vulnerabilities": [' + json.dumps(vulnerability("CVE-2026-0011", "2026-09-16")).encode()
    )
    counted = head.count(b"{") + head.count(b"[") + head.count(b",")
    body = head + b",0" * (nodes - counted) + b"]}"
    assert body.count(b"{") + body.count(b"[") + body.count(b",") == nodes
    return body


def test_a_document_exactly_at_the_ceiling_is_parsed() -> None:
    """The ceiling is a count, not an estimate."""
    (entry,) = parse_kev_catalogue(_padded_to(MAX_NODES)).entries
    assert entry.entry_id == "CVE-2026-0011"


def test_a_well_formed_document_one_node_over_the_ceiling_is_refused() -> None:
    with pytest.raises(DocumentTooComplexError):
        parse_kev_catalogue(_padded_to(MAX_NODES + 1))


@pytest.mark.parametrize(
    "body",
    [
        catalogue(),
        catalogue(
            {"cve": "CVE-2026-0012", "date_added": "2026-09-16"},
            {"cve": "CVE-2026-0013", "date_added": "2026-09-15"},
        ),
    ],
    ids=["empty", "renamed-fields"],
)
def test_a_catalogue_with_nothing_usable_is_a_failure_not_an_empty_success(body: bytes) -> None:
    """The catalogue only grows. Parsing to nothing means it or its schema
    changed, and a quiet success would leave the source reporting ``ok``
    while new entries stopped and retention emptied the feed."""
    with pytest.raises(ParseError, match="none of"):
        parse_kev_catalogue(body)


def test_deep_nesting_inside_the_ceiling_is_refused_on_any_stack() -> None:
    """Whether this depth parses depends on the stack it runs on: it raises
    ``RecursionError`` on the macOS main thread and parsed on the Linux CI
    runner. Either way it is refused, by the recursion handler in the
    first case and by the empty-catalogue check in the second, since
    nested lists yield no entries."""
    body = b'{"vulnerabilities": ' + b"[" * (MAX_NODES - 10) + b"]" * (MAX_NODES - 10) + b"}"
    with pytest.raises(ParseError):
        parse_kev_catalogue(body)


def test_a_recursion_error_is_reported_as_too_complex(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler, pinned without depending on how deep a given stack
    goes before the interpreter refuses."""

    def overflow(_: bytes) -> Any:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(kev, "json", SimpleNamespace(loads=overflow))
    with pytest.raises(DocumentTooComplexError, match="nested too deeply"):
        parse_kev_catalogue(catalogue(vulnerability("CVE-2026-0015", "2026-09-16")))


# --- through the refresh path ---------------------------------------------


@pytest.fixture
def security_topic(db_session: Session) -> Topic:
    row = Topic(slug="security", name="Security")
    db_session.add(row)
    db_session.commit()
    return row


def _source(db_session: Session, topic: Topic, *, slug: str, feed_url: str) -> SourceRef:
    row = Source(
        slug=slug,
        name=slug,
        feed_url=feed_url,
        website_url="https://www.cisa.gov/",
        refresh_minutes=60,
    )
    db_session.add(row)
    db_session.commit()
    db_session.add(SourceTopic(source_id=row.id, topic_id=topic.id))
    db_session.commit()
    return SourceRef(row.id, row.slug, row.feed_url, 60, (topic.id,))


@pytest.fixture
def kev_source(db_session: Session, security_topic: Topic) -> SourceRef:
    return _source(db_session, security_topic, slug="cisa-kev", feed_url=CISA_KEV_FEED_URL)


@pytest.fixture
def service(session_factory: sessionmaker[Session], ingest_settings: Settings) -> IngestService:
    resolver = StubResolver({"www.cisa.gov": [TEST_IP], "feeds.example.com": [TEST_IP]})
    fetcher = FeedFetcher(
        ingest_settings, guard=UrlGuard(resolver=resolver), rate_limiter=HostRateLimiter(0.0)
    )
    return IngestService(session_factory, ingest_settings, fetcher=fetcher)


def _stored(db_session: Session) -> dict[str, FeedItem]:
    return {item.canonical_url: item for item in db_session.scalars(select(FeedItem))}


@respx.mock
def test_refresh_stores_recent_vulnerabilities_only(
    service: IngestService, kev_source: SourceRef, db_session: Session
) -> None:
    respx.get(KEV_PINNED_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=catalogue(
                vulnerability(
                    "CVE-2026-58704",
                    "2026-09-16",
                    shortDescription="<script>alert(1)</script>Modem &lt;b&gt;flaw&lt;/b&gt;",
                ),
                vulnerability("CVE-2026-40001", "2026-07-01"),
                # Outside the 90-day window: stored now, pruned in an hour,
                # re-inserted on the next refresh, for ever.
                vulnerability("CVE-2021-44228", "2021-12-10"),
            ),
        )
    )

    status = service.refresh_source(kev_source, now=NOW)

    assert status.consecutive_failures == 0
    stored = _stored(db_session)
    assert set(stored) == {f"{PAGE}CVE-2026-58704", f"{PAGE}CVE-2026-40001"}
    newest = stored[f"{PAGE}CVE-2026-58704"]
    assert newest.title == "CVE-2026-58704: Example Widget Path Traversal Vulnerability"
    assert newest.summary == "Modem flaw. Federal remediation due 2026-10-07."
    assert newest.published_at.replace(tzinfo=UTC) == datetime(
        2026, 9, 16, 18, 47, 50, 679600, tzinfo=UTC
    )
    assert db_session.scalar(select(func.count()).select_from(FeedItemTopic)) == 2


@respx.mock
def test_a_repeated_refresh_adds_only_what_is_new(
    service: IngestService, kev_source: SourceRef, db_session: Session
) -> None:
    route = respx.get(KEV_PINNED_URL).mock(
        return_value=httpx.Response(
            200, content=catalogue(vulnerability("CVE-2026-40001", "2026-09-14"))
        )
    )
    service.refresh_source(kev_source, now=NOW)

    route.mock(
        return_value=httpx.Response(
            200,
            content=catalogue(
                vulnerability("CVE-2026-40002", "2026-09-17"),
                vulnerability("CVE-2026-40001", "2026-09-14"),
                released="2026-09-17T15:00:00Z",
            ),
        )
    )
    status = service.refresh_source(kev_source, now=NOW)

    assert status.last_inserted_count == 1
    assert set(_stored(db_session)) == {f"{PAGE}CVE-2026-40001", f"{PAGE}CVE-2026-40002"}


@respx.mock
def test_a_json_body_from_any_other_source_is_not_parsed_as_kev(
    service: IngestService, db_session: Session, security_topic: Topic
) -> None:
    """The adapter is chosen by configuration. A source that starts
    answering with a catalogue-shaped body is still an RSS source, and
    fails like one."""
    other = _source(
        db_session, security_topic, slug="example", feed_url="https://feeds.example.com/rss"
    )
    respx.get(PINNED_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=catalogue(vulnerability("CVE-2026-40001", "2026-09-16")),
        )
    )

    status = service.refresh_source(other, now=NOW)

    assert status.consecutive_failures == 1
    assert status.last_error_class == "ParseError"
    assert _stored(db_session) == {}


@respx.mock
def test_the_kev_source_does_not_fall_back_to_rss(
    service: IngestService, kev_source: SourceRef, db_session: Session, rss_feed: bytes
) -> None:
    respx.get(KEV_PINNED_URL).mock(return_value=httpx.Response(200, content=rss_feed))

    status = service.refresh_source(kev_source, now=NOW)

    assert status.consecutive_failures == 1
    assert status.last_error_class == "ParseError"
    assert _stored(db_session) == {}


@respx.mock
def test_an_emptied_catalogue_counts_as_a_failure_and_keeps_what_is_stored(
    service: IngestService, kev_source: SourceRef, db_session: Session
) -> None:
    route = respx.get(KEV_PINNED_URL).mock(
        return_value=httpx.Response(
            200, content=catalogue(vulnerability("CVE-2026-40001", "2026-09-14"))
        )
    )
    service.refresh_source(kev_source, now=NOW)

    route.mock(return_value=httpx.Response(200, content=catalogue()))
    status = service.refresh_source(kev_source, now=NOW)

    assert status.consecutive_failures == 1
    assert status.last_error_class == "ParseError"
    assert set(_stored(db_session)) == {f"{PAGE}CVE-2026-40001"}


def test_the_seeded_source_is_the_one_the_adapter_is_keyed_on() -> None:
    """An exact string match routes the catalogue. A seed URL that differed
    by so much as a trailing slash would send it to the RSS parser."""
    (seeded,) = [source for source in SOURCES if source.slug == "cisa-kev"]
    assert ADAPTERS[seeded.feed_url] is parse_kev_catalogue
    assert seeded.topics == ("security",)
