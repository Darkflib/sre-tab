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

from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.orm import Session

from app.cli.catalogue import SOURCES, TOPICS, SeedSource, medium_source, slug_problem
from app.db.models import (
    FeedItem,
    FeedItemTopic,
    Source,
    SourceStatus,
    SourceTopic,
    Topic,
    TopicOrigin,
)
from app.ingest.language import LanguageDetectionError, detect_language_strict
from app.ingest.store import insert_rule_links
from app.ingest.topicrules import topics_for_url
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


@dataclass(frozen=True)
class RetagReport:
    """What a re-tag pass changed, or would change under ``--dry-run``."""

    items_examined: int
    links_added: int
    links_removed: int

    @property
    def changed(self) -> bool:
        return bool(self.links_added or self.links_removed)


@dataclass(frozen=True)
class LanguageReport:
    """What a detection pass changed, or would change under ``--dry-run``."""

    items_examined: int
    items_changed: int
    #: Items the detector failed on, whose stored language was left alone.
    items_failed: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.items_changed)


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


# --- re-tagging ---------------------------------------------------------

#: Rows read from ``feed_items`` per pass. The whole point of the command
#: is that it runs over the full retention window, which at the PRD's
#: scale is tens of thousands of rows — enough that streaming them in
#: batches is worth the loop and not nearly enough to justify anything
#: cleverer.
RETAG_BATCH = 1000


def retag_items(db: Session, *, dry_run: bool = False) -> RetagReport:
    """Re-derive every rule-owned topic link from the items' URLs.

    This is the path that makes :mod:`app.ingest.topicrules` correctable.
    Ingest only ever *adds* links, so a rule that was wrong, or a rule
    that has since been written, leaves the retained window describing
    itself the way it did when each item arrived. Running this after any
    change to the ruleset brings the whole window into line with it.

    **It touches only what the ruleset owns.** Links with
    ``origin='source'`` are read, to avoid proposing a duplicate of one,
    and are never deleted — the operator's configuration is the authority
    on what a source is about, and a ruleset that could quietly drop it
    would be a worse bargain than the mis-tagging it fixes.

    Reads the whole link table rather than joining per batch. It is the
    smaller of the two tables by a wide margin — a handful of topics per
    item against every item — and holding it means the per-batch work is
    two set differences rather than a query.
    """
    existing: dict[tuple[int, int], TopicOrigin] = {
        (feed_item_id, topic_id): origin
        for feed_item_id, topic_id, origin in db.execute(
            select(FeedItemTopic.feed_item_id, FeedItemTopic.topic_id, FeedItemTopic.origin)
        ).tuples()
    }
    catalogue = dict(db.execute(select(Topic.slug, Topic.id)).tuples().all())

    wanted: set[tuple[int, int]] = set()
    examined = 0
    last_id = 0
    while True:
        batch = (
            db.execute(
                select(FeedItem.id, FeedItem.canonical_url)
                .where(FeedItem.id > last_id)
                .order_by(FeedItem.id)
                .limit(RETAG_BATCH)
            )
            .tuples()
            .all()
        )
        if not batch:
            break
        for item_id, url in batch:
            examined += 1
            for slug in topics_for_url(url):
                topic_id = catalogue.get(slug)
                # A slug the ruleset emits and this instance has no row
                # for. `seed` adds it; until then the link cannot exist,
                # which is a missing tag rather than a broken pass.
                if topic_id is not None:
                    wanted.add((item_id, topic_id))
        last_id = batch[-1][0]

    owned = {pair for pair, origin in existing.items() if origin is TopicOrigin.RULE}
    # A pair the source already asserts is not the ruleset's to add: it is
    # present, it is correct, and inserting it would either conflict or
    # demote it.
    to_add = sorted(wanted - owned - existing.keys())
    to_remove = sorted(owned - wanted)

    if not dry_run:
        for chunk in (
            to_remove[i : i + RETAG_BATCH] for i in range(0, len(to_remove), RETAG_BATCH)
        ):
            db.execute(
                delete(FeedItemTopic).where(
                    tuple_(FeedItemTopic.feed_item_id, FeedItemTopic.topic_id).in_(chunk),
                    FeedItemTopic.origin == TopicOrigin.RULE,
                )
            )
        # Conflict-ignore rather than `add_all`: a refresh running while
        # this does can write the same pair first. See `insert_rule_links`.
        # The count reported below is therefore an upper bound under that
        # race — the pairs this pass found missing, not rows it alone wrote.
        insert_rule_links(db, to_add)
        db.flush()

    return RetagReport(
        items_examined=examined, links_added=len(to_add), links_removed=len(to_remove)
    )


def detect_item_languages(db: Session, *, dry_run: bool = False) -> LanguageReport:
    """Re-detect every retained item's language, writing only what moved.

    Ingest detects a language once, when an item arrives, so this is the
    pass for everything that arrived before detection existed and for
    everything a change to ``app.ingest.language`` would now answer
    differently. It is the whole window rather than only the ``NULL``
    rows, for that second reason: a raised threshold has to be able to
    take an answer back.

    Keyset batches by id, as :func:`retag_items` reads them. No lock is
    needed against a concurrent refresh: ingest only ever inserts, with
    the language already set, and this only updates rows it has just read.
    """
    examined = 0
    failed = 0
    changes: dict[str | None, list[int]] = {}
    last_id = 0
    while True:
        batch = (
            db.execute(
                select(FeedItem.id, FeedItem.title, FeedItem.summary, FeedItem.language)
                .where(FeedItem.id > last_id)
                .order_by(FeedItem.id)
                .limit(RETAG_BATCH)
            )
            .tuples()
            .all()
        )
        if not batch:
            break
        for item_id, title, summary, current in batch:
            examined += 1
            # A failure is skipped, never read as "not sure": that would
            # write NULL over an answer the item already has.
            try:
                detected = detect_language_strict(title, summary)
            except LanguageDetectionError:
                failed += 1
                continue
            if detected != current:
                changes.setdefault(detected, []).append(item_id)
        last_id = batch[-1][0]

    if not dry_run:
        for language, ids in changes.items():
            for start in range(0, len(ids), RETAG_BATCH):
                db.execute(
                    update(FeedItem)
                    .where(FeedItem.id.in_(ids[start : start + RETAG_BATCH]))
                    .values(language=language)
                    .execution_options(synchronize_session=False)
                )
        db.flush()

    return LanguageReport(
        items_examined=examined,
        items_changed=sum(len(ids) for ids in changes.values()),
        items_failed=failed,
    )
