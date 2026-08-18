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

import io
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# defusedxml ships no py.typed and pyproject.toml is Phase 0 property,
# so the override lives here rather than in the mypy config.
import defusedxml.ElementTree as DefusedET  # type: ignore[import-untyped]
import feedparser
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]

from app.ingest.errors import (
    DocumentTooComplexError,
    IngestError,
    ParseError,
    UnsafeDocumentError,
    UnsupportedFeedFormatError,
)

#: feedparser reports e.g. "rss20", "atom10", "rss091n". Anything not
#: starting with one of these is refused.
SUPPORTED_VERSION_PREFIXES = ("rss", "atom")

#: Bound on entries *kept* from one document.
MAX_ENTRIES = 500

#: Bound on entries a document may *contain* before it is refused
#: outright. Ten times what we keep, so a large legitimate feed is never
#: rejected for being large — nothing real ships five thousand items —
#: while a document built to be expensive stops at the gate.
MAX_ENTRY_ELEMENTS = MAX_ENTRIES * 10

#: Bound on total elements, and the one that actually does the work.
#: Entry count alone does not bound a DOM parser: a body made of a
#: million tiny non-entry elements has no entries at all and still costs
#: the same allocation. Measured at roughly 350 bytes of process memory
#: per element, so this ceiling is about 35 MB against a unit capped at
#: MemoryMax=768M.
MAX_ELEMENTS = 100_000

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
    """Reject hostile XML, and anything too big to hand to a DOM parser.

    Streaming, and deliberately so. This used to build a whole
    ``ElementTree`` with ``fromstring`` and throw it away — paying a full
    parse purely as a gate, and then paying it a second time in
    ``feedparser``. The security guarantee does not need the tree: the
    ``forbid_*`` checks fire on parser events, so ``iterparse`` refuses a
    DTD at the point it is declared rather than after the document has
    been materialised.

    What that buys is a bound. ``MAX_ENTRIES`` capped only what was kept
    downstream, never the parse that produced it, so a document sitting
    just under ``source_fetch_max_bytes`` cost about 97 MB and 2.3
    seconds to reduce to 500 entries — against a unit capped at
    ``MemoryMax=768M`` and a serial refresh loop, which is one hostile
    upstream away from being the whole service's problem. Counting nodes
    as they stream, and stopping at the ceiling, costs 0.01s and 1 MB for
    the same document.
    """
    body = content.lstrip(_BOM).lstrip()
    if not body:
        raise ParseError("empty document")

    root: Any = None
    elements = 0
    entries = 0
    try:
        for event, element in DefusedET.iterparse(
            io.BytesIO(body),
            events=("start", "end"),
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        ):
            if event == "start":
                if root is None:
                    root = element
                continue

            elements += 1
            if elements > MAX_ELEMENTS:
                raise DocumentTooComplexError(f"document has more than {MAX_ELEMENTS} elements")
            # Namespace-insensitive: RSS puts items in no namespace, Atom
            # puts entries in one, and a feed is free to declare either.
            if element.tag.rpartition("}")[2] in ("item", "entry"):
                entries += 1
                if entries > MAX_ENTRY_ELEMENTS:
                    raise DocumentTooComplexError(
                        f"document has more than {MAX_ENTRY_ELEMENTS} entries"
                    )

            # Both halves are needed. Clearing the element releases its own
            # children; clearing the root drops the reference the tree
            # still holds to the element itself, which is what keeps this
            # flat rather than merely slower-growing.
            element.clear()
            if element is not root:
                root.clear()
    except DefusedXmlException as exc:
        raise UnsafeDocumentError(f"refused hostile XML construct: {type(exc).__name__}") from exc
    except IngestError:
        raise
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
