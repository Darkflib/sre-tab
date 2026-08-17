"""RSS and Atom parsing, with XML defused before feedparser sees it.

``defusedxml`` runs first as a gate, not as the parser: the body must
survive a parse with DTDs, entity declarations, and external references
all forbidden before ``feedparser`` is handed the original bytes. That
makes billion-laughs and XXE structurally impossible rather than
dependent on feedparser's own settings — a feed carrying any DTD at all
is rejected, which costs nothing in practice and removes the whole class.

RSS and Atom only. Anything else — JSON Feed, CDF, a sitemap, an HTML
page — is a configuration error the operator must fix, not a parser
special case (PLAN, "Deferred to v2").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# defusedxml ships no py.typed and pyproject.toml is Phase 0 property,
# so the override lives here rather than in the mypy config.
import defusedxml.ElementTree as DefusedET  # type: ignore[import-untyped]
import feedparser
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]

from app.ingest.errors import ParseError, UnsafeDocumentError, UnsupportedFeedFormatError

#: feedparser reports e.g. "rss20", "atom10", "rss091n". Anything not
#: starting with one of these is refused.
SUPPORTED_VERSION_PREFIXES = ("rss", "atom")

#: Bound on entries taken from one document, so a hostile feed cannot
#: turn a 5 MB body into unbounded work downstream.
MAX_ENTRIES = 500

_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class ParsedEntry:
    title: str | None
    link: str | None
    summary: str | None
    #: Timezone-aware UTC, or ``None`` when the feed omitted a date or
    #: gave one feedparser could not read.
    published: datetime | None
    image_url: str | None
    entry_id: str | None


@dataclass(frozen=True)
class ParsedFeed:
    version: str
    title: str | None
    entries: tuple[ParsedEntry, ...]


def assert_safe_document(content: bytes) -> None:
    """Reject any XML carrying a DTD, entity, or external reference."""
    body = content.lstrip(_BOM).lstrip()
    if not body:
        raise ParseError("empty document")
    try:
        DefusedET.fromstring(body, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as exc:
        raise UnsafeDocumentError(f"refused hostile XML construct: {type(exc).__name__}") from exc
    except Exception as exc:
        # Not well-formed XML at all. feedparser's lenient mode would
        # sometimes cope, but a feed that is not well-formed XML is not
        # something v1 undertakes to parse.
        raise ParseError(f"not well-formed XML: {type(exc).__name__}: {exc}") from exc


def parse_feed(content: bytes) -> ParsedFeed:
    """Parse *content* as RSS or Atom. Never touches the network."""
    assert_safe_document(content)

    # Bytes, never a string: feedparser treats a string that looks like a
    # URL or path as something to go and fetch.
    parsed = feedparser.parse(content)

    version = str(parsed.get("version") or "")
    if not version.startswith(SUPPORTED_VERSION_PREFIXES):
        raise UnsupportedFeedFormatError(f"feed format {version or 'unknown'!r} is not RSS or Atom")
    if parsed.get("bozo") and not parsed.get("entries"):
        bozo = parsed.get("bozo_exception")
        raise ParseError(f"malformed feed: {type(bozo).__name__ if bozo else 'unknown'}")

    entries = tuple(_entry(raw) for raw in list(parsed.entries)[:MAX_ENTRIES])
    return ParsedFeed(
        version=version,
        title=_text(parsed.feed.get("title")) if parsed.get("feed") else None,
        entries=entries,
    )


def _entry(raw: Any) -> ParsedEntry:
    return ParsedEntry(
        title=_text(raw.get("title")),
        link=_link(raw),
        summary=_summary(raw),
        published=_published(raw),
        image_url=_image(raw),
        entry_id=_text(raw.get("id")),
    )


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _link(raw: Any) -> str | None:
    link = _text(raw.get("link"))
    if link:
        return link
    for candidate in raw.get("links", []) or []:
        if candidate.get("rel") in (None, "alternate") and _text(candidate.get("href")):
            return str(candidate["href"])
    # Atom ids are frequently the canonical URL; RSS guids often are too.
    identifier = _text(raw.get("id"))
    if identifier and identifier.lower().startswith(("http://", "https://")):
        return identifier
    return None


def _summary(raw: Any) -> str | None:
    summary = _text(raw.get("summary"))
    if summary:
        return summary
    for block in raw.get("content", []) or []:
        value = _text(block.get("value"))
        if value:
            return value
    return None


def _published(raw: Any) -> datetime | None:
    """feedparser hands back a ``struct_time`` already in UTC."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = raw.get(key)
        if not parsed:
            continue
        try:
            year, month, day, hour, minute, second = (int(part) for part in parsed[:6])
            return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
        except (TypeError, ValueError):
            continue
    return None


def _image(raw: Any) -> str | None:
    for thumbnail in raw.get("media_thumbnail", []) or []:
        url = _text(thumbnail.get("url"))
        if url:
            return url
    for media in raw.get("media_content", []) or []:
        url = _text(media.get("url"))
        if url and str(media.get("medium", "image")).startswith("image"):
            return url
    for enclosure in raw.get("enclosures", []) or []:
        if str(enclosure.get("type", "")).startswith("image/"):
            url = _text(enclosure.get("href")) or _text(enclosure.get("url"))
            if url:
                return url
    return None
