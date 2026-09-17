"""The CISA Known Exploited Vulnerabilities catalogue, as feed entries.

The one source that is not RSS or Atom, and the first of the bespoke
adapters the v1 plan deferred. It is admitted on narrow terms.

**Chosen by configuration, never by content.** The adapter is bound to
the catalogue's own URL, and the refresh path hands a body to it only
when the source's configured ``feed_url`` is exactly
:data:`CISA_KEV_FEED_URL`. Nothing a response says — its content type, or
a body that happens to open with ``{`` — can move a source onto this
path, so every other source is parsed exactly as it was.

**One item per vulnerability, linked to CISA's record of it.** Not to
NVD: ``feed_items.canonical_url`` is the instance-wide dedup key, and an
item that *is* the KEV entry should not collide with some other source's
link to the same CVE and lose. The catalogue is every entry CISA has
ever added; retention does the rest, since only entries added inside
``feed_retention_days`` are stored, so the feed carries what is new
rather than the back catalogue.

**The parse is bounded, as the XML one is.** ``json.loads`` has no
entity expansion to defuse, but it is still a DOM parser: a body inside
the 5 MiB fetch cap made of ``[{},{},…]`` peaked at 132 MB. Every array
element and every object member follows either an opening bracket or a
comma, so counting ``{``, ``[`` and ``,`` before parsing bounds what the
parse can allocate. It overcounts, since the same characters inside
strings are counted too, which errs towards refusal. Measured at the
ceiling, the worst shapes peaked at 16 MB. The real catalogue counted
26,921 at 1.73 MB, which scales to about 82,000 at the default byte cap,
so that cap refuses a growing catalogue before this ceiling does.

This module only reports what the document said. Every string here is
untrusted and raw, exactly as :mod:`app.ingest.parse` leaves its own, and
:mod:`app.ingest.normalise` makes them safe.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, time
from typing import Any

from app.ingest.errors import DocumentTooComplexError, ParseError
from app.ingest.parse import MAX_ENTRIES, ParsedEntry, ParsedFeed

CISA_KEV_FEED_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
)

#: The catalogue's own page, filtered to one entry by its CVE ID. A
#: plain query parameter rather than a fragment, because the normaliser
#: drops fragments and the ID is what tells one item from the next.
CATALOGUE_PAGE_URL = "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"

#: ``ParsedFeed.version`` for this document, which the refresh log reports.
FORMAT = "cisa-kev"

#: Bound on ``{`` + ``[`` + ``,`` in the raw body; see the module docstring.
#: The same figure as :data:`app.ingest.parse.MAX_ELEMENTS`, and it lands
#: in the same place: tens of megabytes, not hundreds.
MAX_NODES = 100_000

_CVE_ID = re.compile(r"CVE-\d{4}-\d{4,}")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_kev_catalogue(content: bytes) -> ParsedFeed:
    """Parse the KEV catalogue. Never touches the network."""
    nodes = content.count(b"{") + content.count(b"[") + content.count(b",")
    if nodes > MAX_NODES:
        raise DocumentTooComplexError(f"document has more than {MAX_NODES} JSON nodes")

    try:
        # Bytes, so json detects the encoding and tolerates a UTF-8 BOM.
        document = json.loads(content)
    except RecursionError as exc:
        raise DocumentTooComplexError("JSON nested too deeply") from exc
    except ValueError as exc:
        # Covers malformed JSON, undecodable bytes, and an integer past
        # the interpreter's digit limit — all of them a body this does not
        # undertake to read.
        raise ParseError(f"not a JSON document: {type(exc).__name__}: {exc}") from exc

    vulnerabilities = document.get("vulnerabilities") if isinstance(document, dict) else None
    if not isinstance(vulnerabilities, list):
        raise ParseError("not a KEV catalogue: no 'vulnerabilities' list")

    released = _timestamp(document.get("dateReleased"))
    dated = [
        (added, raw)
        for raw in vulnerabilities
        if isinstance(raw, dict) and (added := _date(raw.get("dateAdded"))) is not None
    ]
    # Newest first before the cap, rather than trusting the order the
    # document happens to use: MAX_ENTRIES is far below the catalogue's
    # size, so an oldest-first document would otherwise keep only entries
    # retention is about to discard.
    dated.sort(key=lambda pair: pair[0], reverse=True)

    entries: list[ParsedEntry] = []
    for added, raw in dated:
        entry = _entry(raw, added=added, released=released)
        if entry is not None:
            entries.append(entry)
            if len(entries) == MAX_ENTRIES:
                break

    if not entries:
        # The catalogue only ever grows, so an empty one is never real.
        # Nothing usable means the document or its schema changed, and
        # reporting that as a success would keep the source green while
        # new entries stopped arriving and retention emptied the feed.
        raise ParseError(f"none of {len(vulnerabilities)} KEV catalogue entries is usable")

    return ParsedFeed(
        version=FORMAT,
        title=_text(document.get("title")),
        image_url=None,
        entries=tuple(entries),
    )


def _entry(raw: dict[str, Any], *, added: date, released: datetime | None) -> ParsedEntry | None:
    cve_id = _text(raw.get("cveID"))
    if cve_id is None or not _CVE_ID.fullmatch(cve_id.strip()):
        # The ID is the entry's identity and half of its link. Without a
        # well-formed one there is nothing to point at.
        return None
    cve_id = cve_id.strip()
    name = _text(raw.get("vulnerabilityName"))
    return ParsedEntry(
        title=f"{cve_id}: {name.strip()}" if name else cve_id,
        link=f"{CATALOGUE_PAGE_URL}?search_api_fulltext={cve_id}",
        summary=_summary(raw),
        published=_published(added, released),
        image_url=None,
        entry_id=cve_id,
    )


def _summary(raw: dict[str, Any]) -> str | None:
    """The description, then the two facts a reader triages on.

    ``requiredAction`` is left out. It is overwhelmingly the same
    directive boilerplate on every entry, several hundred characters of
    it, and would crowd the description out of a card.
    """
    parts: list[str] = []
    description = _text(raw.get("shortDescription"))
    if description:
        description = description.strip()
        parts.append(description if description.endswith((".", "!", "?")) else f"{description}.")
    if (_text(raw.get("knownRansomwareCampaignUse")) or "").strip().lower() == "known":
        parts.append("Known to be used in ransomware campaigns.")
    due = _date(raw.get("dueDate"))
    if due is not None:
        parts.append(f"Federal remediation due {due.isoformat()}.")
    return " ".join(parts) or None


def _published(added: date, released: datetime | None) -> datetime:
    """When the entry appeared, as closely as the document says.

    ``dateAdded`` is a date alone, and midnight would file an entry added
    this evening beneath everything published since this morning, which
    is where a reader looking for new entries does not look. An entry
    added on the day the catalogue was released gets the release time
    instead. The comparison is on the UTC date, so a release late enough
    in the US evening to fall on the next UTC day misses it and takes
    midnight; that costs position, not correctness.
    """
    if released is not None and released.date() == added:
        return released
    return datetime.combine(added, time.min, tzinfo=UTC)


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _date(value: Any) -> date | None:
    text = _text(value)
    if text is None or not _ISO_DATE.fullmatch(text.strip()):
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def _timestamp(value: Any) -> datetime | None:
    text = _text(value)
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
