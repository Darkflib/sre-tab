"""The v1 seed catalogue, from PLAN-v1.md.

Data, not behaviour: :mod:`app.cli.operations` writes it. Two things here
are load-bearing rather than cosmetic.

**The slugs.** ``app.services.preferences.DEFAULT_SOURCE_SLUGS`` is
``("hacker-news", "lobsters", "dev-to", "lwn")`` and is intersected with
what the instance actually holds. Rename one of those four here and every
new user lands on an empty default selection with no error anywhere —
which is why ``tests/test_catalogue.py`` asserts the two agree.

**Medium is a template, not a source.** Each tag becomes its own ordinary
``sources`` row at configuration time, via ``add-medium-tag``. A
runtime-templated URL would put an untrusted path component back in the
fetch path, and acceptance criterion 5 ("invalid/unsafe fetch targets are
rejected before any network request") rests on every fetchable URL being
one an operator explicitly configured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Widened past developer news because the catalogue is: BBC and the
#: Guardian are in it, so the taxonomy needs general-news topics too.
TOPICS: tuple[tuple[str, str], ...] = (
    ("webdev", "Web development"),
    ("python", "Python"),
    ("devops", "DevOps"),
    ("security", "Security"),
    ("open-source", "Open source"),
    ("ai-ml", "AI and machine learning"),
    ("hardware", "Hardware"),
    ("tech-industry", "Tech industry"),
    ("science", "Science"),
    ("uk-news", "UK news"),
    ("world-news", "World news"),
)


@dataclass(frozen=True)
class SeedSource:
    slug: str
    name: str
    feed_url: str
    website_url: str
    refresh_minutes: int
    topics: tuple[str, ...] = field(default_factory=tuple)


SOURCES: tuple[SeedSource, ...] = (
    SeedSource(
        slug="hacker-news",
        name="Hacker News",
        feed_url="https://news.ycombinator.com/rss",
        website_url="https://news.ycombinator.com/",
        refresh_minutes=15,
        topics=("tech-industry",),
    ),
    SeedSource(
        slug="lobsters",
        name="Lobsters",
        feed_url="https://lobste.rs/rss",
        website_url="https://lobste.rs/",
        refresh_minutes=30,
        topics=("open-source", "tech-industry"),
    ),
    SeedSource(
        slug="dev-to",
        name="Dev.to",
        feed_url="https://dev.to/feed",
        website_url="https://dev.to/",
        refresh_minutes=30,
        topics=("webdev",),
    ),
    SeedSource(
        slug="lwn",
        name="LWN",
        feed_url="https://lwn.net/headlines/newrss",
        website_url="https://lwn.net/",
        refresh_minutes=60,
        topics=("open-source", "security"),
    ),
    SeedSource(
        slug="ars-technica",
        name="Ars Technica",
        feed_url="https://feeds.arstechnica.com/arstechnica/index/",
        website_url="https://arstechnica.com/",
        refresh_minutes=30,
        topics=("tech-industry", "science"),
    ),
    SeedSource(
        slug="bbc-news",
        name="BBC News",
        feed_url="https://feeds.bbci.co.uk/news/rss.xml",
        website_url="https://www.bbc.co.uk/news",
        refresh_minutes=15,
        topics=("uk-news", "world-news"),
    ),
    SeedSource(
        slug="guardian-uk",
        name="Guardian UK",
        feed_url="https://www.theguardian.com/uk/rss",
        website_url="https://www.theguardian.com/uk",
        refresh_minutes=30,
        topics=("uk-news",),
    ),
)

MEDIUM_REFRESH_MINUTES = 60

#: The shape every source and topic slug must have: lower-case
#: alphanumerics joined by single interior hyphens.
#:
#: A slug is not an inert label. It is written into the browser's query
#: string, joined into the client's paged-resource cache key, and matched
#: against ``Source.slug``/``Topic.slug`` in the feed query — three
#: consumers that each assume something different about its shape. A slug
#: containing the client's separators is split in two on the way through
#: the URL, so the operator gets a source the feed cannot filter to and no
#: error anywhere. Enforcing the shape at the point the operator chooses
#: it is the same trade ``validate_feed_url`` makes above: fail where the
#: mistake was made, not three components downstream.
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: ``sources.slug`` and ``topics.slug`` are ``String(64)``. PostgreSQL
#: rejects a longer value and SQLite accepts it, so an unchecked slug is
#: also a dialect divergence that only shows up in production.
SLUG_MAX_LENGTH = 64


def slug_problem(value: str) -> str | None:
    """Why ``value`` is not a usable slug, or ``None`` if it is."""
    if not value:
        return "must not be empty"
    if len(value) > SLUG_MAX_LENGTH:
        return f"must be at most {SLUG_MAX_LENGTH} characters, got {len(value)}"
    if not SLUG_PATTERN.match(value):
        return "must be lower-case letters and digits, joined by single hyphens"
    return None


class InvalidMediumTag(ValueError):
    """Raised for a tag that would not make a safe, stable slug."""


def medium_source(tag: str, *, topics: tuple[str, ...] = ()) -> SeedSource:
    """Expand the Medium template into an ordinary source row.

    The tag is checked against the slug pattern first. That check is the
    whole point of doing the expansion at configuration time: what ends up
    in ``sources.feed_url`` is a fixed string an operator chose, not a
    value assembled while a fetch is in flight.

    The composed ``medium-<tag>`` slug is checked as well as the tag. The
    prefix costs seven characters, so a tag that is itself within the
    column's limit could still produce a slug that is not — accepted by
    SQLite in development and refused by PostgreSQL in production.
    """
    normalised = tag.strip().lower()
    if not normalised or not SLUG_PATTERN.match(normalised):
        raise InvalidMediumTag(
            f"{tag!r} is not a valid Medium tag: lower-case letters, digits, "
            "and single hyphens only"
        )

    slug = f"medium-{normalised}"
    problem = slug_problem(slug)
    if problem is not None:
        raise InvalidMediumTag(f"{tag!r} makes an unusable slug {slug!r}: it {problem}")

    return SeedSource(
        slug=slug,
        name=f"Medium — {normalised}",
        feed_url=f"https://medium.com/feed/tag/{normalised}",
        website_url=f"https://medium.com/tag/{normalised}",
        refresh_minutes=MEDIUM_REFRESH_MINUTES,
        topics=topics,
    )
