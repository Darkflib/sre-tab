"""RSS and Atom parsing, and the XML attacks that must not be possible."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.ingest.errors import (
    DocumentTooComplexError,
    ParseError,
    UnsafeDocumentError,
    UnsupportedFeedFormatError,
)
from app.ingest.parse import (
    MAX_ATTRIBUTES_PER_ELEMENT,
    MAX_ELEMENTS,
    MAX_ENTRIES,
    MAX_ENTRY_ELEMENTS,
    parse_feed,
)

XXE = b"""<?xml version="1.0"?>
<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<rss version="2.0"><channel><title>&xxe;</title></channel></rss>"""

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<rss version="2.0"><channel><title>&lol3;</title></channel></rss>"""

EXTERNAL_DTD = b"""<?xml version="1.0"?>
<!DOCTYPE rss SYSTEM "https://attacker.example/evil.dtd">
<rss version="2.0"><channel><title>t</title></channel></rss>"""


# --- hostile XML --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "body"),
    [("xxe", XXE), ("billion-laughs", BILLION_LAUGHS), ("external-dtd", EXTERNAL_DTD)],
)
def test_hostile_xml_is_refused(name: str, body: bytes) -> None:
    with pytest.raises(UnsafeDocumentError):
        parse_feed(body)


def test_empty_body_is_a_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_feed(b"")


def test_not_xml_is_a_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_feed(b"<html><body>this is a web page</body></html>")


def test_html_page_is_not_a_feed() -> None:
    with pytest.raises(ParseError):
        parse_feed(b"<?xml version='1.0'?><html><body><p>hello</p></body></html>")


def test_json_feed_is_refused() -> None:
    with pytest.raises(ParseError):
        parse_feed(b'{"version": "https://jsonfeed.org/version/1", "items": []}')


def test_sitemap_is_not_a_feed() -> None:
    sitemap = (
        b"<?xml version='1.0'?>"
        b"<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
        b"<url><loc>https://example.org/a</loc></url></urlset>"
    )
    with pytest.raises(UnsupportedFeedFormatError):
        parse_feed(sitemap)


# --- RSS ----------------------------------------------------------------


def test_rss_is_parsed(rss_feed: bytes) -> None:
    parsed = parse_feed(rss_feed)
    assert parsed.version.startswith("rss")
    assert parsed.title == "Example Feed"
    assert len(parsed.entries) == 2

    first = parsed.entries[0]
    assert first.title == "First article"
    assert first.link == "https://example.org/first"
    assert first.published == datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    assert first.summary is not None and "enough" in first.summary


def test_rss_with_a_bom_is_parsed(rss_feed: bytes) -> None:
    assert len(parse_feed(b"\xef\xbb\xbf" + rss_feed).entries) == 2


# --- Atom ---------------------------------------------------------------


def test_atom_is_parsed(atom_feed: bytes) -> None:
    parsed = parse_feed(atom_feed)
    assert parsed.version.startswith("atom")
    assert [entry.title for entry in parsed.entries] == ["Atom one", "Atom two"]
    assert parsed.entries[0].link == "https://example.org/atom-one"
    assert parsed.entries[0].published == datetime(2026, 8, 17, 8, 30, tzinfo=UTC)
    # No <published>, so <updated> is the fallback within the parser.
    assert parsed.entries[1].published == datetime(2026, 8, 17, 7, 0, tzinfo=UTC)


def test_atom_link_falls_back_to_the_links_collection() -> None:
    body = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>t</title><id>urn:x</id><updated>2026-08-17T09:00:00Z</updated>
  <entry>
    <title>Only a links element</title>
    <id>urn:uuid:1234</id>
    <link rel="alternate" type="text/html" href="https://example.org/alt"/>
    <updated>2026-08-17T09:00:00Z</updated>
  </entry>
</feed>"""
    assert parse_feed(body).entries[0].link == "https://example.org/alt"


# --- bounds -------------------------------------------------------------


def test_entry_count_is_bounded() -> None:
    items = "".join(
        f"<item><title>t{n}</title><link>https://example.org/{n}</link></item>"
        for n in range(MAX_ENTRIES + 50)
    )
    body = (
        f"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>{items}</channel></rss>"
    ).encode()
    assert len(parse_feed(body).entries) == MAX_ENTRIES


def _rss(inner: str) -> bytes:
    return (
        f"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>{inner}</channel></rss>"
    ).encode()


def test_a_document_over_the_entry_ceiling_is_refused_not_truncated() -> None:
    """The byte cap is not a work cap.

    ``MAX_ENTRIES`` bounds what is kept, never the parse that produced
    it: a document just under ``source_fetch_max_bytes`` used to cost
    about 97 MB and 2.3 seconds to reduce to 500 entries, against a unit
    capped at ``MemoryMax=768M`` and a serial refresh loop. Refusing is
    the right answer rather than truncating, because nothing legitimate
    ships ten times what we would keep.
    """
    body = _rss(
        "<item><title>t</title><link>https://example.org/x</link></item>" * (MAX_ENTRY_ELEMENTS + 1)
    )
    with pytest.raises(DocumentTooComplexError):
        parse_feed(body)


def test_an_element_bomb_with_no_entries_is_refused() -> None:
    """Entry count alone does not bound a DOM parser.

    This document has zero entries and would sail past any entry-based
    ceiling, while costing exactly what the entries would have.
    """
    with pytest.raises(DocumentTooComplexError):
        parse_feed(_rss("<a><b/></a>" * MAX_ELEMENTS))


def test_an_attribute_bomb_on_one_element_is_refused() -> None:
    """The bound that stops a stall rather than an allocation.

    feedparser is quadratic in the attribute count of a single element:
    20,000 attributes on one tag is 0.21 MB and 2.27s, and 60,000 is
    0.65 MB and 21.3s — an eighth of a permitted body stopping a serial
    refresh cycle for twenty seconds. Node counting does not see it,
    because there is only one element; the streaming gate reaches the
    same 60,000 attributes in 0.03s, so the gate is where it belongs.
    """
    attributes = " ".join(f'a{n}="v"' for n in range(MAX_ATTRIBUTES_PER_ELEMENT + 1))
    with pytest.raises(DocumentTooComplexError):
        parse_feed(_rss(f"<item {attributes}><link>https://example.org/x</link></item>"))


def test_ordinary_attribute_use_is_unaffected() -> None:
    """The cap has to be invisible to anything real.

    `<enclosure>` is the usual reason an entry carries attributes at all,
    and three of them is the shape of a podcast feed rather than an
    attack.
    """
    body = _rss(
        "<item><title>x</title><link>https://example.org/x</link>"
        "<enclosure url='https://example.org/a.mp3' length='1' type='audio/mpeg'/></item>"
    )
    assert len(parse_feed(body).entries) == 1


def test_deep_nesting_is_refused_before_the_tree_is_built() -> None:
    """End events arrive innermost-first, so counting there is too late.

    A document that only nests produces no end event until it has
    stopped descending. Measured on 200,000 nested tags — 1.40 MB, well
    inside every byte limit here — the first end arrived after 200,002
    starts with 57 MB already allocated, and an end-counted ceiling had
    not been consulted once.
    """
    depth = MAX_ELEMENTS + 1
    with pytest.raises(DocumentTooComplexError):
        parse_feed(_rss("<a>" * depth + "</a>" * depth))


def test_a_large_but_legitimate_feed_still_parses() -> None:
    """The ceiling must not be reachable by anything real.

    The failure mode being guarded against here is a bound tight enough
    to start refusing ordinary sources, which would be reported as a
    broken feed rather than as a limit doing its job.
    """
    items = "".join(
        f"<item><title>t{n}</title><link>https://example.org/{n}</link></item>"
        for n in range(MAX_ENTRIES - 1)
    )
    assert len(parse_feed(_rss(items)).entries) == MAX_ENTRIES - 1


def test_entry_without_a_date_reports_none() -> None:
    body = (
        b"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>"
        b"<item><title>No date</title><link>https://example.org/x</link></item>"
        b"</channel></rss>"
    )
    assert parse_feed(body).entries[0].published is None


def test_media_thumbnail_becomes_the_image() -> None:
    body = (
        b"<?xml version='1.0'?>"
        b"<rss version='2.0' xmlns:media='http://search.yahoo.com/mrss/'>"
        b"<channel><title>t</title><item><title>i</title>"
        b"<link>https://example.org/x</link>"
        b"<media:thumbnail url='https://cdn.example.org/x.jpg'/>"
        b"</item></channel></rss>"
    )
    assert parse_feed(body).entries[0].image_url == "https://cdn.example.org/x.jpg"
