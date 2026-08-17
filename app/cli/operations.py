"""Operator operations on the source and topic catalogue.

Every function takes a :class:`~sqlalchemy.orm.Session` and does not
commit — the caller owns the transaction (AGENTS.md, "Transactions").
:mod:`app.cli` opens the session and commits.

Feed URLs are validated here rather than at the call site, because the
validation is the interesting part: :meth:`UrlGuard.check_static` is the
whole SSRF guard minus DNS, so a URL that would be refused at fetch time
is refused at ``add`` time instead — where an operator can read the
reason and fix it, rather than discovering it as a failing source hours
later.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.cli.catalogue import SOURCES, TOPICS, SeedSource, medium_source, slug_problem
from app.db.models import Source, SourceStatus, SourceTopic, Topic
from app.ingest.urlguard import UrlGuard, assert_supported_endpoint

_GUARD = UrlGuard()


class OperatorError(Exception):
    """A problem the operator can fix; the CLI prints it without a
    traceback."""


def _require_slug(slug: str, kind: str) -> None:
    """Refuse a slug the rest of the system cannot round-trip.

    Same reasoning as the feed-URL check above, one field along: the
    operator is the one who can fix it, and they can only fix it while
    they are still looking at the command they typed. Deferred, the
    symptom is a source that lists correctly and filters to nothing.
    """
    problem = slug_problem(slug)
    if problem is not None:
        raise OperatorError(f"{kind} slug {slug!r} is not usable: it {problem}")


@dataclass(frozen=True)
class SourceView:
    slug: str
    name: str
    feed_url: str
    enabled: bool
    refresh_minutes: int
    topics: tuple[str, ...]


@dataclass(frozen=True)
class StatusView:
    slug: str
    name: str
    enabled: bool
    refresh_minutes: int
    last_fetched_at: datetime | None
    last_success_at: datetime | None
    last_error_class: str | None
    last_error_detail: str | None
    consecutive_failures: int

    @property
    def state(self) -> str:
        if not self.enabled:
            return "disabled"
        if self.last_fetched_at is None:
            return "never fetched"
        return "ok" if self.consecutive_failures == 0 else f"failing ({self.consecutive_failures})"


@dataclass(frozen=True)
class SeedReport:
    topics_added: tuple[str, ...]
    sources_added: tuple[str, ...]
    topic_links_added: int

    @property
    def changed(self) -> bool:
        return bool(self.topics_added or self.sources_added or self.topic_links_added)


# --- validation ---------------------------------------------------------


def validate_feed_url(raw_url: str) -> str:
    """Config-time URL validation. Returns the normalised URL to store.

    No DNS, so this is safe to run against a URL nobody has approved yet
    and cheap enough to run on every write. The endpoint check is called
    explicitly as well as through ``check_static``: GraphQL and sitemap
    endpoints are the v2 deferral an operator is most likely to trip
    over, and naming it here is what makes the refusal legible.

    ``check_static`` is applied to its own output, which is not
    belt-and-braces. It normalises the host — trailing dot stripped, case
    folded — and a host can *become* an obfuscated IP literal only once
    that has happened: ``https://0x7f.0.0.1./rss`` is an ordinary-looking
    name on the first pass and ``0x7f.0.0.1`` on the second, which is
    127.0.0.1. Fetch time catches these anyway, because ``validate``
    re-judges the normalised host as a literal before resolving — but
    catching it hours earlier, at ``source add``, is the entire point of
    this function, and demanding that validation be a fixpoint is what
    makes that true for the whole family rather than one URL at a time.
    """
    try:
        url = _GUARD.check_static(raw_url)
        url = _GUARD.check_static(str(url))
        assert_supported_endpoint(url)
    except Exception as exc:
        raise OperatorError(f"refused feed URL: {exc}") from exc
    return str(url)


# --- topics -------------------------------------------------------------


def list_topics(db: Session) -> list[Topic]:
    return list(db.scalars(select(Topic).order_by(Topic.slug)))


def add_topic(db: Session, *, slug: str, name: str) -> Topic:
    _require_slug(slug, "topic")
    if db.scalar(select(Topic).where(Topic.slug == slug)) is not None:
        raise OperatorError(f"topic {slug!r} already exists")
    topic = Topic(slug=slug, name=name)
    db.add(topic)
    db.flush()
    return topic


def set_topic_enabled(db: Session, slug: str, *, enabled: bool) -> Topic:
    topic = db.scalar(select(Topic).where(Topic.slug == slug))
    if topic is None:
        raise OperatorError(f"no topic {slug!r}")
    topic.enabled = enabled
    db.flush()
    return topic


# --- sources ------------------------------------------------------------


def list_sources(db: Session) -> list[SourceView]:
    sources = list(db.scalars(select(Source).order_by(Source.slug)))
    links: dict[int, list[str]] = {}
    for source_id, slug in db.execute(
        select(SourceTopic.source_id, Topic.slug).join(Topic, Topic.id == SourceTopic.topic_id)
    ):
        links.setdefault(source_id, []).append(slug)
    return [
        SourceView(
            slug=source.slug,
            name=source.name,
            feed_url=source.feed_url,
            enabled=source.enabled,
            refresh_minutes=source.refresh_minutes,
            topics=tuple(sorted(links.get(source.id, []))),
        )
        for source in sources
    ]


def add_source(
    db: Session,
    *,
    slug: str,
    name: str,
    feed_url: str,
    website_url: str,
    refresh_minutes: int,
    topics: Sequence[str] = (),
    icon_url: str | None = None,
) -> Source:
    _require_slug(slug, "source")
    if db.scalar(select(Source).where(Source.slug == slug)) is not None:
        raise OperatorError(f"source {slug!r} already exists")
    if refresh_minutes < 1:
        raise OperatorError("refresh interval must be at least one minute")

    source = Source(
        slug=slug,
        name=name,
        feed_url=validate_feed_url(feed_url),
        website_url=website_url,
        refresh_minutes=refresh_minutes,
        icon_url=icon_url,
    )
    db.add(source)
    db.flush()
    _link_topics(db, source, topics)
    return source


def add_medium_tag(db: Session, tag: str, *, topics: Sequence[str] = ()) -> Source:
    """Expand ``medium.com/feed/tag/<tag>`` into an ordinary source row."""
    seed = medium_source(tag, topics=tuple(topics))
    return add_source(
        db,
        slug=seed.slug,
        name=seed.name,
        feed_url=seed.feed_url,
        website_url=seed.website_url,
        refresh_minutes=seed.refresh_minutes,
        topics=seed.topics or topics,
    )


def set_source_enabled(db: Session, slug: str, *, enabled: bool) -> Source:
    source = db.scalar(select(Source).where(Source.slug == slug))
    if source is None:
        raise OperatorError(f"no source {slug!r}")
    source.enabled = enabled
    db.flush()
    return source


def set_source_topics(db: Session, slug: str, topics: Sequence[str]) -> Source:
    source = db.scalar(select(Source).where(Source.slug == slug))
    if source is None:
        raise OperatorError(f"no source {slug!r}")
    db.execute(delete(SourceTopic).where(SourceTopic.source_id == source.id))
    _link_topics(db, source, topics)
    return source


# --- status -------------------------------------------------------------


def nonconforming_slugs(db: Session) -> list[tuple[str, str, str]]:
    """Rows whose slug predates the format check, as ``(kind, slug, why)``.

    Enforcement at ``add`` time only binds what is added after it, and a
    slug cannot be rewritten in place without breaking every saved
    selection that names it. So the existing catalogue is reported rather
    than migrated, and reported somewhere an operator already looks.
    """
    found: list[tuple[str, str, str]] = []
    for kind, model in (("source", Source), ("topic", Topic)):
        for slug in db.scalars(select(model.slug).order_by(model.slug)):
            problem = slug_problem(slug)
            if problem is not None:
                found.append((kind, slug, problem))
    return found


def refresh_status(db: Session) -> list[StatusView]:
    """What every configured source last did, read from ``source_status``.

    A separate process from the one that did the fetching, which is the
    reason the table exists: the in-process registry can only answer for
    the replica that owns it.
    """
    rows = db.execute(
        select(Source, SourceStatus)
        .outerjoin(SourceStatus, SourceStatus.source_id == Source.id)
        .order_by(Source.slug)
    ).all()
    return [
        StatusView(
            slug=source.slug,
            name=source.name,
            enabled=source.enabled,
            refresh_minutes=source.refresh_minutes,
            last_fetched_at=status.last_fetched_at if status else None,
            last_success_at=status.last_success_at if status else None,
            last_error_class=status.last_error_class if status else None,
            last_error_detail=status.last_error_detail if status else None,
            consecutive_failures=status.consecutive_failures if status else 0,
        )
        for source, status in rows
    ]


# --- seeding ------------------------------------------------------------


def seed_catalogue(db: Session, sources: Sequence[SeedSource] = SOURCES) -> SeedReport:
    """Install the v1 topics and sources. Idempotent.

    Existing rows are left exactly as they are — an operator who renamed a
    source, changed its interval, or disabled it has made a decision, and
    re-running the seed is not the place to undo it. Only missing rows and
    missing topic links are added.
    """
    known_topics = {topic.slug: topic for topic in db.scalars(select(Topic))}
    topics_added: list[str] = []
    for slug, name in TOPICS:
        if slug not in known_topics:
            topic = Topic(slug=slug, name=name)
            db.add(topic)
            known_topics[slug] = topic
            topics_added.append(slug)
    db.flush()

    known_sources = {source.slug: source for source in db.scalars(select(Source))}
    sources_added: list[str] = []
    links_added = 0
    for seed in sources:
        source = known_sources.get(seed.slug)
        if source is None:
            source = Source(
                slug=seed.slug,
                name=seed.name,
                feed_url=validate_feed_url(seed.feed_url),
                website_url=seed.website_url,
                refresh_minutes=seed.refresh_minutes,
            )
            db.add(source)
            db.flush()
            sources_added.append(seed.slug)
        links_added += _link_topics(db, source, seed.topics)

    db.flush()
    return SeedReport(
        topics_added=tuple(topics_added),
        sources_added=tuple(sources_added),
        topic_links_added=links_added,
    )


def _link_topics(db: Session, source: Source, topics: Sequence[str]) -> int:
    """Attach topic slugs to a source. Returns the number of new links.

    Every item ingested from this source inherits these topics, so an
    unlinked source produces items with no topics — invisible under an
    explicit ``?topics=`` filter, which is literal by design.
    """
    if not topics:
        return 0

    known = dict(
        db.execute(select(Topic.slug, Topic.id).where(Topic.slug.in_(topics))).tuples().all()
    )
    unknown = sorted(set(topics) - known.keys())
    if unknown:
        raise OperatorError(f"unknown topic slugs: {', '.join(unknown)}")

    existing = set(
        db.scalars(select(SourceTopic.topic_id).where(SourceTopic.source_id == source.id))
    )
    added = 0
    for slug in dict.fromkeys(topics):
        topic_id = known[slug]
        if topic_id not in existing:
            db.add(SourceTopic(source_id=source.id, topic_id=topic_id))
            added += 1
    db.flush()
    return added
