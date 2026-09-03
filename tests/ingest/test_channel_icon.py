"""The channel artwork a feed declares, from the document to the card.

`sources.icon_url` has existed since Phase 0, `FeedSourceRef` carries it,
`ItemCard` renders it, and nothing ever set it — the affordance was built
and never fired. This is what fires it.

Three separable claims, and they are tested apart because they fail apart:
that the parser finds the artwork wherever the format puts it, that the
write records it without stepping on the operator's own value, and that a
refresh which finds nothing does not erase what a previous one found.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Source, SourceStatus
from app.ingest.parse import parse_feed
from app.ingest.service import IngestService, SourceRef
from app.ingest.store import record_discovered_icon
from tests.ingest.conftest import PINNED_URL, rss_body

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

ITEM = """    <item>
      <title>One</title>
      <link>https://example.org/one</link>
    </item>"""


def atom(*, logo: str = "", icon: str = "") -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <id>urn:example</id>
  <updated>2026-08-17T09:00:00Z</updated>
  {logo}
  {icon}
  <entry>
    <title>One</title>
    <id>urn:one</id>
    <link href="https://example.org/atom-one"/>
    <updated>2026-08-17T09:00:00Z</updated>
  </entry>
</feed>""".encode()


# --- parsing ------------------------------------------------------------


def test_rss_channel_image_is_found() -> None:
    body = rss_body(ITEM).replace(
        b"<description>Example</description>",
        b"<description>Example</description>\n"
        b"    <image><url>https://cdn.example.org/logo.png</url>"
        b"<title>Example</title><link>https://example.org/</link></image>",
    )

    assert parse_feed(body).image_url == "https://cdn.example.org/logo.png"


def test_atom_logo_is_preferred_over_icon() -> None:
    """The Atom specification separates them by size and intent — a logo is
    the publication's banner, an icon is a favicon-sized mark. This value is
    read at both sizes, and only the larger one survives being a card's
    fallback image."""
    parsed = parse_feed(
        atom(
            logo="<logo>https://cdn.example.org/logo.png</logo>",
            icon="<icon>https://cdn.example.org/icon.png</icon>",
        )
    )

    assert parsed.image_url == "https://cdn.example.org/logo.png"


def test_atom_icon_is_used_when_there_is_no_logo() -> None:
    parsed = parse_feed(atom(icon="<icon>https://cdn.example.org/icon.png</icon>"))

    assert parsed.image_url == "https://cdn.example.org/icon.png"


def test_a_feed_declaring_no_artwork_reports_none() -> None:
    assert parse_feed(rss_body(ITEM)).image_url is None
    assert parse_feed(atom()).image_url is None


# --- the write ----------------------------------------------------------


@pytest.fixture
def bare_source(db_session: Session) -> Source:
    """A source of this module's own, deliberately not named ``source``:
    the ingest conftest owns that name and builds ``source_ref`` from it,
    so shadowing it silently pointed the end-to-end test below at a
    different feed — which then failed the URL guard rather than the
    assertion, and said so."""
    source = Source(
        slug="iconic",
        name="Iconic",
        feed_url="https://iconic.example/rss",
        website_url="https://iconic.example",
    )
    db_session.add(source)
    db_session.commit()
    return source


def _stored(db_session: Session, source: Source) -> str | None:
    row = db_session.get(SourceStatus, source.id)
    return row.discovered_icon_url if row else None


def test_the_icon_is_recorded_when_no_status_row_exists_yet(
    db_session: Session, bare_source: Source
) -> None:
    """A source polled for the first time writes its items before the
    status registry writes its row, so this has to be an upsert."""
    assert db_session.get(SourceStatus, bare_source.id) is None

    assert record_discovered_icon(db_session, source_id=bare_source.id, icon_url="https://a/1.png")
    db_session.commit()

    assert _stored(db_session, bare_source) == "https://a/1.png"


def test_a_changed_icon_replaces_the_stored_one(db_session: Session, bare_source: Source) -> None:
    record_discovered_icon(db_session, source_id=bare_source.id, icon_url="https://a/1.png")

    assert record_discovered_icon(db_session, source_id=bare_source.id, icon_url="https://a/2.png")
    db_session.commit()

    assert _stored(db_session, bare_source) == "https://a/2.png"


def test_an_unchanged_icon_is_not_rewritten(db_session: Session, bare_source: Source) -> None:
    """Reported rather than written. Every refresh of every source carries
    the same artwork, so an unconditional UPDATE would be one write per
    source per poll for a value that changes about never."""
    record_discovered_icon(db_session, source_id=bare_source.id, icon_url="https://a/1.png")

    assert (
        record_discovered_icon(db_session, source_id=bare_source.id, icon_url="https://a/1.png")
        is False
    )


def test_a_refresh_finding_nothing_does_not_erase_what_one_found_before(
    db_session: Session, bare_source: Source
) -> None:
    """`None` is an absence, not a value. A missing `<image>` element is far
    more often a truncated fetch than a publisher retiring their logo, and
    blanking the card on one bad poll is the worse mistake."""
    record_discovered_icon(db_session, source_id=bare_source.id, icon_url="https://a/1.png")
    db_session.commit()

    assert record_discovered_icon(db_session, source_id=bare_source.id, icon_url=None) is False
    db_session.commit()

    assert _stored(db_session, bare_source) == "https://a/1.png"


def test_the_status_row_keeps_its_refresh_state_when_the_icon_changes(
    db_session: Session, bare_source: Source
) -> None:
    """The two writers share a row and must not clobber one another: the
    icon rides the item write and the timings ride the status registry."""
    db_session.add(
        SourceStatus(source_id=bare_source.id, last_error_class="Timeout", consecutive_failures=3)
    )
    db_session.commit()

    record_discovered_icon(db_session, source_id=bare_source.id, icon_url="https://a/1.png")
    db_session.commit()

    row = db_session.get(SourceStatus, bare_source.id)
    assert row is not None
    assert row.discovered_icon_url == "https://a/1.png"
    assert row.consecutive_failures == 3
    assert row.last_error_class == "Timeout"


# --- end to end ---------------------------------------------------------


@respx.mock
def test_a_refresh_records_the_channel_artwork(
    ingest_service: IngestService, source_ref: SourceRef, db_session: Session
) -> None:
    body = rss_body(ITEM).replace(
        b"<description>Example</description>",
        b"<description>Example</description>\n"
        b"    <image><url>https://cdn.example.org/logo.png</url>"
        b"<title>Example</title><link>https://example.org/</link></image>",
    )
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=body))

    ingest_service.refresh_source(source_ref, now=NOW)

    row = db_session.get(SourceStatus, source_ref.id)
    assert row is not None
    assert row.discovered_icon_url == "https://cdn.example.org/logo.png"


@respx.mock
@pytest.mark.parametrize(
    "hostile",
    [
        "http://127.0.0.1/logo.png",
        "https://10.0.0.1/logo.png",
        "javascript:alert(1)",
        "https://localhost/logo.png",
    ],
)
def test_a_hostile_channel_image_url_is_refused(
    ingest_service: IngestService, source_ref: SourceRef, db_session: Session, hostile: str
) -> None:
    """The channel image goes through the same guard every other
    feed-supplied URL goes through. It is rendered in a browser with the
    reader's credentials ambient, so "it is only an icon" is not a reason
    to trust it less carefully than an item's own image."""
    body = rss_body(ITEM).replace(
        b"<description>Example</description>",
        f"<description>Example</description>\n    <image><url>{hostile}</url>"
        f"<title>x</title><link>https://example.org/</link></image>".encode(),
    )
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=body))

    ingest_service.refresh_source(source_ref, now=NOW)

    row = db_session.get(SourceStatus, source_ref.id)
    assert row is None or row.discovered_icon_url is None


@respx.mock
def test_a_failed_refresh_records_no_artwork(
    ingest_service: IngestService, source_ref: SourceRef, db_session: Session
) -> None:
    respx.get(PINNED_URL).mock(return_value=httpx.Response(500))

    ingest_service.refresh_source(source_ref, now=NOW)

    assert _stored(db_session, db_session.scalars(select(Source)).one()) is None
