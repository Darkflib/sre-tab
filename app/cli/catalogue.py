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

#: A Medium tag becomes a path component of a URL that is then stored and
#: fetched, so it is validated here and never templated at runtime.
_MEDIUM_TAG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MEDIUM_TAG_MAX = 64


class InvalidMediumTag(ValueError):
    """Raised for a tag that would not make a safe, stable slug."""


def medium_source(tag: str, *, topics: tuple[str, ...] = ()) -> SeedSource:
    """Expand the Medium template into an ordinary source row.

    The tag is checked against a strict slug pattern first. That check is
    the whole point of doing the expansion at configuration time: what
    ends up in ``sources.feed_url`` is a fixed string an operator chose,
    not a value assembled while a fetch is in flight.
    """
    normalised = tag.strip().lower()
    if not normalised or len(normalised) > _MEDIUM_TAG_MAX or not _MEDIUM_TAG.match(normalised):
        raise InvalidMediumTag(
            f"{tag!r} is not a valid Medium tag: lower-case letters, digits, "
            "and single hyphens only"
        )
    return SeedSource(
        slug=f"medium-{normalised}",
        name=f"Medium — {normalised}",
        feed_url=f"https://medium.com/feed/tag/{normalised}",
        website_url=f"https://medium.com/tag/{normalised}",
        refresh_minutes=MEDIUM_REFRESH_MINUTES,
        topics=topics,
    )
