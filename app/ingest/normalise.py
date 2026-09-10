"""Turn a parsed entry into the row shape ``feed_items`` expects.

Two things here carry weight beyond tidiness.

**Summaries are plain text.** Feed HTML never reaches the database.
``nh3`` strips markup, and because stripping leaves entity references
that would decode *back* into markup (``&lt;script&gt;`` → ``<script>``)
the clean/unescape pair runs twice and a final sweep removes anything
still tag-shaped. What is stored is text, and the API contract is that
it is rendered as text.

**Canonical URLs are normalised before comparison**, because
``feed_items.canonical_url`` is the instance-wide dedup key. The rules,
and why each is drawn where it is:

===========================  =============================================
Scheme                       Lower-cased. ``http`` and ``https`` are kept
                             **distinct** — collapsing them would merge
                             two URLs that are not provably the same
                             resource, and the cost of not merging is one
                             duplicate card.
Host                         Lower-cased, IDNA-encoded to punycode, any
                             trailing dot removed. A host that is an IP
                             literal is refused: no legitimate article
                             link is one.
Port                         Default port for the scheme removed. Any
                             other port preserved.
Path                         Percent-encoding normalised, ``.``/``..``
                             segments resolved, empty path becomes ``/``.
                             **Trailing slashes are preserved.** ``/a``
                             and ``/a/`` are genuinely different resources
                             on plenty of servers; under-normalising costs
                             a duplicate card, over-normalising merges two
                             different articles into one and loses the
                             second permanently.
Query                        Tracking parameters removed (``utm_*``,
                             ``at_*``, ``ns_*``, ``fbclid``, ``CMP``,
                             Medium's ``source``, and the rest of
                             :data:`TRACKING_PARAMETERS`); the remainder
                             sorted by key so parameter order cannot
                             create a duplicate.
Fragment                     Removed entirely. A fragment addresses a
                             position within a page, never a different
                             article.
Credentials                  An item URL carrying ``user:pass@`` is
                             refused outright.
===========================  =============================================

Published times are timezone-aware UTC. The documented fallback when a
feed omits a date, or gives one feedparser cannot read, is **the fetch
time** — which also caps absurd future dates, so a feed claiming 2099
cannot pin itself to the top of everyone's list forever.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode

import httpx
import nh3

from app.ingest.parse import ParsedEntry
from app.ingest.urlguard import parse_numeric_ipv4

MAX_TITLE_LENGTH = 1024
MAX_SUMMARY_LENGTH = 2000
MAX_URL_LENGTH = 2048

#: How far ahead of the fetch time a publication date may claim to be
#: before it is treated as unusable.
MAX_CLOCK_SKEW = timedelta(days=1)

#: Removed before dedup comparison. Prefix matches first, then exact.
TRACKING_PARAMETER_PREFIXES = ("utm_", "at_", "ns_", "pk_", "mtm_", "stm_", "piwik_")
TRACKING_PARAMETERS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "twclid",
        "ttclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "cmp",
        "ref",
        "ref_src",
        "ref_url",
        "referrer",
        "spm",
        "source",
        "guccounter",
        "guce_referrer",
        "guce_referrer_sig",
        "__twitter_impression",
        "wt.mc_id",
        "wtmc",
    }
)

_PERCENT_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")
_TAG_LIKE = re.compile(r"<[^>]*>")
_WHITESPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CONTENT_TAGS = {"script", "style", "iframe", "object", "embed"}


class InvalidItemURLError(ValueError):
    """An entry's link cannot be used as a canonical URL."""


@dataclass(frozen=True)
class NormalisedItem:
    canonical_url: str
    title: str
    summary: str | None
    published_at: datetime
    image_url: str | None


# --- text ---------------------------------------------------------------


def to_plain_text(raw: str | None) -> str | None:
    """Strip markup to plain text. Never returns anything tag-shaped."""
    if raw is None:
        return None
    text = raw
    # Twice: nh3 escapes what it leaves behind, and unescaping that can
    # reveal markup the first pass never saw.
    for _ in range(2):
        text = nh3.clean(
            text,
            tags=set(),
            attributes={},
            clean_content_tags=_CONTENT_TAGS,
            strip_comments=True,
        )
        text = html.unescape(text)
    text = _TAG_LIKE.sub(" ", text)
    text = _CONTROL.sub("", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text or None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# --- urls ---------------------------------------------------------------


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered in TRACKING_PARAMETERS or lowered.startswith(TRACKING_PARAMETER_PREFIXES)


def _clean_query(raw_query: str) -> str:
    if not raw_query:
        return ""
    pairs = [
        (key, value)
        for key, value in parse_qsl(raw_query, keep_blank_values=True)
        if not _is_tracking(key)
    ]
    if not pairs:
        return ""
    return urlencode(sorted(pairs))


def normalise_url(raw: str) -> str:
    """Canonical form for dedup. Raises :class:`InvalidItemURLError`."""
    candidate = (raw or "").strip()
    if not candidate or len(candidate) > MAX_URL_LENGTH:
        raise InvalidItemURLError("empty or over-long url")
    if any(ch in candidate for ch in "\r\n\t"):
        raise InvalidItemURLError("control character in url")

    try:
        url = httpx.URL(candidate)
    except (httpx.InvalidURL, ValueError) as exc:
        raise InvalidItemURLError(str(exc)) from exc

    if url.scheme not in ("http", "https"):
        raise InvalidItemURLError(f"scheme {url.scheme!r} is not http(s)")
    if url.userinfo:
        raise InvalidItemURLError("credentials in url")

    host = _feed_url_host(url.raw_host)
    if host is None:
        raise InvalidItemURLError("invalid item url host")

    netloc = host if url.port is None else f"{host}:{url.port}"
    raw_path = url.raw_path.decode("ascii")
    # RFC 3986 normal form: percent escapes upper-case. httpx preserves
    # whatever the feed wrote, which would make %c3 and %C3 two items.
    path = (
        _PERCENT_ESCAPE.sub(lambda match: match.group().upper(), raw_path.split("?", 1)[0]) or "/"
    )
    query = _clean_query(url.query.decode("ascii"))

    normalised = f"{url.scheme}://{netloc}{path}"
    if query:
        normalised = f"{normalised}?{query}"
    if len(normalised) > MAX_URL_LENGTH:
        raise InvalidItemURLError("normalised url exceeds the column width")
    return normalised


def _looks_like_ip(host: str) -> bool:
    """True if *host* is an IP literal — dotted, obfuscated, or IPv6.

    ``:`` cannot appear in a DNS label, so its presence alone identifies
    an IPv6 literal (``raw_host`` never carries the surrounding
    brackets). Everything else — including the obfuscated IPv4 forms
    (octal, hex, bare-decimal, short ``inet_aton`` forms) — is judged by
    :func:`app.ingest.urlguard.parse_numeric_ipv4`. That decoder already
    exists for the SSRF guard on outbound feed fetches, which needs the
    exact same answer to a differently-shaped question: not "may the
    fetcher connect to this address" but "is this host shape even a
    hostname" — the parsing is identical either way, so it is shared
    rather than copied a second time.
    """
    return ":" in host or parse_numeric_ipv4(host) is not None


def _feed_url_host(raw_host: bytes | None) -> str | None:
    """Shared host rules for any URL taken from an untrusted feed.

    Applies to both the canonical item URL (:func:`normalise_url`, which
    raises on rejection) and the optional image URLs
    (:func:`safe_optional_url`, which returns ``None``): they must
    not disagree about what a feed-supplied host is allowed to be, so
    both call this rather than keeping their own copy of the rule.

    Returns the normalised host — lower-cased, trailing dot stripped —
    or ``None`` if it has no dot or is an IP literal in any form
    :func:`_looks_like_ip` recognises. The IP check runs on the
    *normalised* host, not the raw one: an obfuscated literal can carry
    a trailing dot that gets it past httpx's own parser, so the literal
    only becomes visible once that dot is stripped and the case folded.
    """
    if not raw_host:
        return None
    host = raw_host.decode("ascii").rstrip(".").lower()
    if not host or "." not in host:
        return None
    if _looks_like_ip(host):
        return None
    return host


def safe_optional_url(raw: str | None) -> str | None:
    """An optional feed-supplied URL, or ``None`` if it cannot be trusted.

    Public because two callers now need it: an entry's image, and the
    channel's own artwork. Both are decorative URLs from an untrusted
    document, and both must answer to the same host rules the canonical
    URL answers to — see :func:`_feed_url_host`.
    """
    if not raw:
        return None
    try:
        url = httpx.URL(raw.strip())
    except (httpx.InvalidURL, ValueError):
        return None
    if url.scheme not in ("http", "https") or url.userinfo:
        return None
    if _feed_url_host(url.raw_host) is None:
        return None
    text = str(url)
    return text if len(text) <= MAX_URL_LENGTH else None


# --- time ---------------------------------------------------------------


def normalise_published(published: datetime | None, *, fetched_at: datetime) -> datetime:
    """UTC, never naive, never implausibly far in the future."""
    if published is None:
        return fetched_at
    moment = published.astimezone(UTC) if published.tzinfo else published.replace(tzinfo=UTC)
    if moment > fetched_at + MAX_CLOCK_SKEW:
        return fetched_at
    return moment


# --- entries ------------------------------------------------------------


def normalise_entry(entry: ParsedEntry, *, fetched_at: datetime) -> NormalisedItem | None:
    """Row shape for one entry, or ``None`` if it is unusable.

    Returning ``None`` rather than raising is deliberate: one malformed
    entry must not cost the rest of the feed.
    """
    if entry.link is None:
        return None
    try:
        canonical_url = normalise_url(entry.link)
    except InvalidItemURLError:
        return None

    title = to_plain_text(entry.title) or canonical_url
    summary = to_plain_text(entry.summary)
    return NormalisedItem(
        canonical_url=canonical_url,
        title=_truncate(title, MAX_TITLE_LENGTH),
        summary=_truncate(summary, MAX_SUMMARY_LENGTH) if summary else None,
        published_at=normalise_published(entry.published, fetched_at=fetched_at),
        image_url=safe_optional_url(entry.image_url),
    )


def normalise_entries(
    entries: tuple[ParsedEntry, ...], *, fetched_at: datetime, oldest_allowed: datetime | None
) -> list[NormalisedItem]:
    """Normalise, drop unusable and out-of-retention entries, dedup.

    Items older than the retention window are dropped here rather than
    stored and pruned an hour later — otherwise every refresh would
    re-insert what the prune job just deleted.
    """
    seen: set[str] = set()
    items: list[NormalisedItem] = []
    for entry in entries:
        item = normalise_entry(entry, fetched_at=fetched_at)
        if item is None or item.canonical_url in seen:
            continue
        if oldest_allowed is not None and item.published_at < oldest_allowed:
            continue
        seen.add(item.canonical_url)
        items.append(item)
    return items
