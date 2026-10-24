"""Static snapshots of chosen sources, for sites that show them publicly.

sre-tab's API needs a credential, so a public page cannot call it. This
builds the alternative: a JSON document a timer writes into a volume and
a static web server serves, so a visitor's request never reaches sre-tab
and the serving container holds no token.

**Only what an operator names is published.** The caller passes source
slugs explicitly; nothing is inferred from topics, and an empty list is
refused before a query runs. Items come from ``feed_items`` and the two
source tables alone, so no reader's state — read marks, bookmarks,
preferences — is reachable from here, whatever the document grows to.

**Every value is re-checked on the way out.** Titles and summaries were
made plain text at ingest, but the document is read by pages that did
not write it, so text is cleaned and capped again and a link that is not
https on its source's own host is dropped rather than published.

Two documents are built:

- the *public* snapshot (:data:`PUBLIC_FORMAT_VERSION`): the latest
  items of each named source, with each source's last successful refresh
  so a reader can age each one separately;
- the *KEV* snapshot (:data:`KEV_FORMAT_VERSION`): the CISA catalogue in
  exactly the shape darkflib.com's ``kev-snapshot`` writes and its
  ``src/kev/snapshot.ts`` reads, so that site can be pointed here
  without a change on its side.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import FeedItem, Source
from app.ingest.kev import CISA_KEV_FEED_URL

#: Bumped when a reader of the public snapshot would have to change.
PUBLIC_FORMAT_VERSION = 1
#: darkflib.com's ``FormatVersion``. Not ours to bump: it changes only
#: together with that site's reader.
KEV_FORMAT_VERSION = 1
KEV_SNAPSHOT_NAME = "kev.json"

MAX_TITLE_CHARS = 300
MAX_SUMMARY_CHARS = 600
MAX_SOURCE_NAME_CHARS = 200
# darkflib.com's maxNameRunes and maxDescriptionRunes.
MAX_KEV_NAME_CHARS = 200
MAX_KEV_DESCRIPTION_CHARS = 800
#: Retention keeps 60 to 90 KEV entries; this is headroom, not a target.
MAX_KEV_ENTRIES = 1000

# The shapes app/ingest/kev.py writes: "<CVE>: <vulnerabilityName>", and
# a summary of the description, then an optional ransomware sentence,
# then an optional deadline sentence, in that order. A test feeds the
# ingest side through this parse, so the two cannot drift apart quietly.
_KEV_TITLE = re.compile(r"(CVE-\d{4}-\d{4,})(?::\s+(.+))?", re.DOTALL)
_KEV_DUE = re.compile(r"(?:^|\s)Federal remediation due (\d{4}-\d{2}-\d{2})\.$")
_KEV_RANSOMWARE = "Known to be used in ransomware campaigns."
_KEV_URL_HOST = "www.cisa.gov"

_SNAPSHOT_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


class ExportError(Exception):
    """Nothing usable to publish; the previous file is left in place."""


@dataclass(frozen=True)
class KevFields:
    cve: str
    name: str
    description: str
    due: str | None
    ransomware: bool


@dataclass
class ExportStats:
    """What a build kept and dropped, per source slug, for the operator."""

    kept: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)

    def count(self, slug: str, *, kept: bool) -> None:
        bucket = self.kept if kept else self.dropped
        bucket[slug] = bucket.get(slug, 0) + 1


# --- text and values -------------------------------------------------------


def clean_text(text: str, limit: int) -> str:
    """Collapse whitespace, drop control and format characters, cap at *limit*.

    Format characters (``Cf``) include the bidirectional overrides, which
    would let one item's text reorder what a page renders around it.
    """
    out: list[str] = []
    pending_space = False
    for char in text:
        if char.isspace():
            pending_space = bool(out)
            continue
        if unicodedata.category(char) in ("Cc", "Cf") or char == "\ufffd":
            continue
        if pending_space:
            out.append(" ")
            pending_space = False
        out.append(char)
    cleaned = "".join(out)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _utc(moment: datetime) -> datetime:
    # SQLite hands timezone-aware columns back naive; they were stored as UTC.
    return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)


def iso_timestamp(moment: datetime) -> str:
    """Second precision with a ``Z``, as Go's ``time.Time`` marshals a UTC time."""
    return _utc(moment).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _host(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        return None
    return (parts.hostname or "").rstrip(".").lower() or None


def _bare(host: str) -> str:
    return host.removeprefix("www.")


def https_on_hosts(url: str | None, hosts: frozenset[str]) -> str | None:
    """*url* if it is https on one of *hosts*, ignoring a ``www.`` prefix, else None."""
    if not url:
        return None
    host = _host(url)
    if host is None or _bare(host) not in {_bare(allowed) for allowed in hosts}:
        return None
    return url


def source_hosts(source: Source) -> frozenset[str]:
    """The hosts a source's items may link to: its feed's and its website's."""
    return frozenset(host for url in (source.feed_url, source.website_url) if (host := _host(url)))


def is_kev_source(source: Source) -> bool:
    # The same test ingest uses to pick the KEV adapter: configuration, never content.
    return source.feed_url == CISA_KEV_FEED_URL


def parse_kev_item(title: str, summary: str | None) -> KevFields | None:
    """Take apart the title and summary :mod:`app.ingest.kev` wrote.

    A summary that does not end the expected way is kept whole as the
    description, so a wording change costs the structured fields, not
    the text.
    """
    match = _KEV_TITLE.fullmatch(title.strip())
    if match is None:
        return None
    description = (summary or "").strip()
    due: str | None = None
    if (due_match := _KEV_DUE.search(description)) is not None:
        candidate = due_match.group(1)
        try:
            date.fromisoformat(candidate)
        except ValueError:
            pass
        else:
            due = candidate
            description = description[: due_match.start()].strip()
    ransomware = False
    if description.endswith(_KEV_RANSOMWARE):
        rest = description.removesuffix(_KEV_RANSOMWARE)
        if not rest or rest.endswith(" "):
            ransomware = True
            description = rest.strip()
    return KevFields(
        cve=match.group(1),
        name=clean_text(match.group(2) or "", MAX_KEV_NAME_CHARS),
        description=clean_text(description, MAX_KEV_DESCRIPTION_CHARS),
        due=due,
        ransomware=ransomware,
    )


# --- queries ----------------------------------------------------------------


def load_sources(db: Session, slugs: Sequence[str]) -> list[Source]:
    """The named sources in the order given. Unknown slugs are simply absent.

    Refuses an empty list rather than reading it as "nothing to filter".
    """
    if not slugs:
        raise ValueError("at least one source slug is required")
    found = {
        source.slug: source
        for source in db.scalars(
            select(Source).where(Source.slug.in_(slugs)).options(selectinload(Source.status))
        )
    }
    return [found[slug] for slug in dict.fromkeys(slugs) if slug in found]


def _latest(db: Session, source: Source, limit: int) -> list[FeedItem]:
    return list(
        db.scalars(
            select(FeedItem)
            .where(FeedItem.source_id == source.id)
            .order_by(FeedItem.published_at.desc(), FeedItem.id.desc())
            .limit(limit)
        )
    )


def _last_success(source: Source) -> datetime | None:
    return source.status.last_success_at if source.status is not None else None


# --- documents ------------------------------------------------------------


def build_public_snapshot(
    db: Session, sources: Sequence[Source], *, per_source: int
) -> tuple[dict[str, Any], ExportStats]:
    """The latest *per_source* items of each source, newest first overall.

    Raises :class:`ExportError` when no source yields a usable item:
    publishing an empty document would blank every page reading it, when
    the previous one is still the better answer.
    """
    if per_source < 1:
        raise ValueError("per_source must be at least 1")
    stats = ExportStats()
    described: dict[str, dict[str, Any]] = {}
    items: list[tuple[datetime, int, dict[str, Any]]] = []

    for source in sources:
        hosts = source_hosts(source)
        last_success = _last_success(source)
        described[source.slug] = {
            "name": clean_text(source.name, MAX_SOURCE_NAME_CHARS),
            "website_url": https_on_hosts(source.website_url, hosts),
            "last_success_at": iso_timestamp(last_success) if last_success else None,
        }
        kev = is_kev_source(source)
        for row in _latest(db, source, per_source):
            url = https_on_hosts(row.canonical_url, hosts)
            title = clean_text(row.title, MAX_TITLE_CHARS)
            if url is None or not title:
                stats.count(source.slug, kept=False)
                continue
            item: dict[str, Any] = {
                "source": source.slug,
                "title": title,
                "summary": clean_text(row.summary, MAX_SUMMARY_CHARS) if row.summary else None,
                "url": url,
                "published": iso_timestamp(row.published_at),
            }
            if kev:
                fields = parse_kev_item(row.title, row.summary)
                if fields is not None:
                    item["kev"] = {
                        "cve": fields.cve,
                        "due": fields.due,
                        "ransomware": fields.ransomware,
                    }
            stats.count(source.slug, kept=True)
            items.append((_utc(row.published_at), row.id, item))

    if not items:
        raise ExportError(f"none of {len(sources)} source(s) has a publishable item")
    items.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    document = {
        "version": PUBLIC_FORMAT_VERSION,
        "sources": described,
        "items": [item for _, _, item in items],
    }
    return document, stats


def build_kev_snapshot(db: Session, source: Source) -> tuple[dict[str, Any], int]:
    """darkflib.com's ``kev.json``, from the KEV source's retained items.

    ``fetched_at`` is the source's last successful refresh, not the time
    of this export. darkflib.com ages the panel by it, so a stalled ingest
    shows as stale data rather than being re-stamped fresh every hour.

    Returns the document and the number of items dropped.
    """
    last_success = _last_success(source)
    if last_success is None:
        raise ExportError(f"{source.slug} has never refreshed successfully")

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    dropped = 0
    for row in _latest(db, source, MAX_KEV_ENTRIES):
        fields = parse_kev_item(row.title, row.summary)
        if fields is None or fields.cve in seen:
            dropped += 1
            continue
        seen.add(fields.cve)
        entry: dict[str, Any] = {
            "cve": fields.cve,
            "name": fields.name,
            "description": fields.description,
            "added": iso_timestamp(row.published_at),
        }
        # Field order and omission follow kev-snapshot's Go struct tags.
        if fields.due:
            entry["due"] = fields.due
        entry["ransomware"] = fields.ransomware
        # Exactly www.cisa.gov, which is what darkflib.com's reader admits.
        if _host(row.canonical_url) == _KEV_URL_HOST:
            entry["url"] = row.canonical_url
        entries.append(entry)

    if not entries:
        raise ExportError(f"none of {dropped} {source.slug} item(s) is a usable KEV entry")
    entries.sort(key=lambda entry: (entry["added"], entry["cve"]), reverse=True)
    document = {
        "version": KEV_FORMAT_VERSION,
        "fetched_at": iso_timestamp(last_success),
        "entries": entries,
    }
    return document, dropped


# --- writing ----------------------------------------------------------------


def snapshot_filename(name: str) -> str:
    """``<name>.json`` for a lower-case slug-shaped *name*, else ValueError."""
    if not _SNAPSHOT_NAME.fullmatch(name):
        raise ValueError(f"{name!r} is not a lower-case name of letters, digits and hyphens")
    return f"{name}.json"


def encode(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def write_atomic(directory: Path, filename: str, data: bytes) -> bool:
    """Replace ``directory/filename`` with *data*; False if it already held it.

    Skipping an identical write keeps the file's mtime, and so the ETag a
    web server derives from it, stable between refreshes that changed
    nothing. The temporary file is a dotfile, which the serving side is
    configured not to expose.
    """
    target = directory / filename
    try:
        if target.read_bytes() == data:
            return False
    except FileNotFoundError:
        pass

    descriptor, temporary = tempfile.mkstemp(
        dir=directory, prefix=f".{target.stem}-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise

    # The rename is durable only once the directory entry is.
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return True
