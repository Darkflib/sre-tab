"""Topics derived from an item's own URL, additive to its source's.

``upsert_items`` asserts the *source's* topics onto every item it writes,
which is why a cricket video from the BBC's news feed carries ``uk-news``
and ``world-news`` and nothing else. This module is the correction: where
a publisher puts its own section in the path, that section is read and
mapped onto the catalogue's slugs.

Three decisions are load-bearing here.

**Keyed by host, not by source.** The roadmap entry proposed keying the
ruleset by source slug, on the grounds that the seed catalogue already
has that shape. Host is strictly better and costs nothing. An aggregator
links to the *article*, so a Lobsters or Hacker News item pointing at
``arstechnica.com/security/...`` gets ``security`` for free, which
source-keying could never express — the source is Lobsters, and Lobsters
has no sections. It also removes a join: the re-tag pass reads
``feed_items.canonical_url`` and needs nothing else.

**The first path segment only.** Every section this ruleset recognises is
the first segment on every source measured, so matching deeper would add
a dimension no rule uses. A deeper match is a change to
:func:`topics_for_url`, not a change to every entry in :data:`SECTIONS`.

**Literal segments, never patterns.** The segment is looked up in a dict.
A regex over a feed-supplied path would put a backtracking engine in the
ingest loop and in the re-tag pass, which is a denial-of-service shape
this project refuses everywhere else it appears (see
``app.ingest.parse``). There is nothing a pattern would buy: publishers'
sections are a closed vocabulary that changes about once a year.

What this does **not** do is remove anything. A rule adds topics beside
the source's; it never contradicts them. ``/politics/`` on the Guardian
genuinely is UK news, so a rule that took ``uk-news`` off it would be
wrong in the same direction as the behaviour it is fixing. Subtraction is
a per-rule opt-in to weigh once there is evidence it is wanted.

Coverage is deliberately partial. The BBC serves most of its journalism
from ``/news/articles/<opaque>``, which names no section at all, and
those items genuinely are UK and world news and are already tagged
correctly. The rule to write is not "classify every item" but
"reclassify the ones the publisher has already classified".
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

#: Section slug to catalogue slugs, per host. The host key is the
#: registrable form this module compares against — lower-cased, with a
#: leading ``www.`` removed (see :func:`link_host`) — so one entry covers
#: ``bbc.co.uk`` and ``www.bbc.co.uk`` both.
#:
#: A host absent from this mapping has no rules, which is the right answer
#: for three different reasons and worth naming so nobody "fixes" it:
#: dev.to is ``dev.to/<author>/<slug>``, so its first segment is a person
#: and a generic rule would mint one topic per author for ever; Hacker
#: News and Lobsters link outward, so the host is the publisher's and is
#: matched on its own terms; and LWN's paths are article ids.
#:
#: Every slug on the right-hand side must exist in
#: ``app.cli.catalogue.TOPICS`` or the link cannot be written. That is not
#: left to inspection — ``tests/cli/test_catalogue.py`` asserts it.
SECTIONS: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    # 32 items in one fetch: 26 under /news/articles/ or /news/videos/
    # with an opaque id and no section, and four under /sport/ — which
    # were the cricket, football, and athletics items that prompted this.
    "bbc.co.uk": {
        "sport": ("sport",),
    },
    # 137 items in one fetch, of which nine were actually under
    # /uk-news/. The section is the first segment on every one of them.
    "theguardian.com": {
        "artanddesign": ("culture",),
        "books": ("culture",),
        "business": ("business",),
        "commentisfree": ("opinion",),
        "culture": ("culture",),
        "education": ("society",),
        "environment": ("environment",),
        "fashion": ("lifestyle",),
        "film": ("culture",),
        "food": ("lifestyle",),
        "football": ("sport",),
        "games": ("culture",),
        "global-development": ("world-news",),
        "lifeandstyle": ("lifestyle",),
        "money": ("business",),
        "music": ("culture",),
        "politics": ("politics",),
        "science": ("science",),
        "society": ("society",),
        "sport": ("sport",),
        "stage": ("culture",),
        "technology": ("tech-industry",),
        "thefilter": ("lifestyle",),
        "travel": ("lifestyle",),
        "tv-and-radio": ("culture",),
        # Redundant for the Guardian's own feed, whose source row already
        # asserts `uk-news` — the rule's link conflicts away and the source
        # keeps it. It is here for the aggregators: keyed by host, a Hacker
        # News item linking to a Guardian /uk-news/ story would otherwise
        # get every section but this one.
        "uk-news": ("uk-news",),
        "us-news": ("world-news",),
        "wellness": ("lifestyle",),
        "world": ("world-news",),
    },
    # The quietest win of the three. Ars carries `tech-industry` and
    # `science` from its source row, so an AI story and a car review are
    # indistinguishable today; its sections are clean first segments and
    # map almost entirely onto topics the catalogue already has.
    "arstechnica.com": {
        "ai": ("ai-ml",),
        "cars": ("hardware",),
        "culture": ("culture",),
        "gadgets": ("hardware",),
        "gaming": ("culture",),
        "health": ("science",),
        "science": ("science",),
        "security": ("security",),
        "space": ("science",),
        "tech-policy": ("tech-industry",),
    },
}


def link_host(url: httpx.URL) -> str | None:
    """The host to look up in :data:`SECTIONS`, or ``None``.

    ``www.`` is stripped because a publisher serves the same sections
    either way and two entries that must be kept in step is a way to get
    one of them wrong. Nothing else is stripped: ``feeds.bbci.co.uk`` is a
    different service from ``bbc.co.uk`` and is not assumed to share a
    path vocabulary with it.

    Public because URL mutes compare hosts by this same rule
    (``app.services.preferences.url_mute_term``). A mute for
    ``theguardian.com/football`` and a rule keyed ``theguardian.com`` are
    two answers to "is this the same host", and they must not disagree.
    """
    raw = url.raw_host
    if not raw:
        return None
    host = raw.decode("ascii").lower()
    return host.removeprefix("www.") or None


def topics_for_url(canonical_url: str) -> tuple[str, ...]:
    """Catalogue slugs this URL's own path argues for. Possibly empty.

    Empty is the ordinary answer, not a failure: a host with no rules, a
    section with no mapping, and a path with no segments all return it,
    and the item keeps exactly the topics its source gave it.

    Takes the canonical URL rather than a parsed object because both
    callers have one — ingest has just written it and the re-tag pass has
    just read it — and because it is the *stored* value, so what this
    function sees is what the feed will be filtered by.
    """
    try:
        url = httpx.URL(canonical_url)
    except (httpx.InvalidURL, ValueError):
        # Unreachable through ingest, which normalises before storing, but
        # this is also run over rows written by earlier versions and by
        # hand. An unparseable URL earns no topics rather than an
        # exception that would stop a re-tag pass on its first bad row.
        return ()

    # Checked here rather than assumed from the caller. `normalise_url`
    # refuses anything but http(s) before a canonical URL is stored, so
    # this cannot fire through ingest — but a function that reads a URL
    # answers for the URL it was given, and the same rule is applied by
    # `safe_optional_url` one module over for the same reason.
    if url.scheme not in ("http", "https"):
        return ()

    host = link_host(url)
    if host is None:
        return ()
    sections = SECTIONS.get(host)
    if not sections:
        return ()

    path = url.path.lstrip("/")
    if not path:
        return ()
    segment = path.split("/", 1)[0].lower()
    return sections.get(segment, ())


def rule_slugs() -> frozenset[str]:
    """Every catalogue slug any rule can emit.

    Used by the tests that hold :data:`SECTIONS` and the seed catalogue in
    agreement, and by the re-tag pass, which resolves this set to ids once
    rather than per item.
    """
    return frozenset(
        slug for sections in SECTIONS.values() for slugs in sections.values() for slug in slugs
    )
