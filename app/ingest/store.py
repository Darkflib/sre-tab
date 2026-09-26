"""Idempotent writes for ingested items — acceptance criterion 2.

Re-fetching a feed must not duplicate items, and must not resurrect or
reorder ones already stored. So the write is insert-or-ignore on
``feed_items.canonical_url``: an item that exists is left exactly as it
is, including its ``published_at``, which is what the feed's ordering
depends on. Nothing here ever updates or deletes an existing row.

The existence check is advisory — it keeps the common re-fetch cheap —
and the ``ON CONFLICT DO NOTHING`` behind it is what makes the write
correct when two replicas race.

Flush, never commit. These take a session rather than opening one, and
whoever opened it owns the transaction (AGENTS.md, "Transactions") — for
the refresh path that is :class:`app.ingest.service.IngestService`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, Executable, delete, insert, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Base, Bookmark, FeedItem, FeedItemTopic, SourceStatus, Topic, TopicOrigin
from app.ingest.language import detect_language
from app.ingest.normalise import NormalisedItem
from app.ingest.topicrules import topics_for_url

log = structlog.get_logger("app.ingest.store")

#: Keeps ``IN (...)`` parameter counts inside SQLite's limit.
CHUNK_SIZE = 400


def _chunks(values: Sequence[str], size: int = CHUNK_SIZE) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _insert_ignore(
    session: Session,
    model: type[Base],
    rows: list[dict[str, object]],
    *,
    index_elements: list[str],
) -> None:
    """``INSERT ... ON CONFLICT DO NOTHING`` on both supported engines."""
    if not rows:
        return
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement: Executable = (
            postgres_insert(model)
            .values(rows)
            .on_conflict_do_nothing(index_elements=index_elements)
        )
    elif dialect == "sqlite":
        statement = (
            sqlite_insert(model).values(rows).on_conflict_do_nothing(index_elements=index_elements)
        )
    else:
        # Neither production nor development uses anything else; fall
        # back to one savepoint per row rather than failing outright.
        for row in rows:
            try:
                with session.begin_nested():
                    session.execute(insert(model).values(row))
            except IntegrityError:
                continue
        return
    session.execute(statement)


def _link_source_topics(session: Session, rows: list[dict[str, object]]) -> None:
    """Assert the source's topic links, promoting a rule's where they meet.

    ``ON CONFLICT DO UPDATE`` rather than ``DO NOTHING``, and that is the
    difference between the precedence rule being true and it being an
    accident of insertion order. A rule may already have derived the same
    ``(item, topic)`` pair from the item's URL; if the operator then adds
    that topic to the source, the pair has to become the operator's,
    because a re-tag deletes what the ruleset owns and would otherwise
    take an operator's assertion with it the first time the rule changed.

    The promotion is one-way. Nothing ever rewrites a ``source`` row back
    to ``rule``: the rule's claim adds no information once the operator
    has made the same one explicitly.
    """
    if not rows:
        return
    dialect = session.get_bind().dialect.name
    index_elements = ["feed_item_id", "topic_id"]
    if dialect == "postgresql":
        statement: Executable = (
            postgres_insert(FeedItemTopic)
            .values(rows)
            .on_conflict_do_update(
                index_elements=index_elements, set_={"origin": TopicOrigin.SOURCE.value}
            )
        )
    elif dialect == "sqlite":
        statement = (
            sqlite_insert(FeedItemTopic)
            .values(rows)
            .on_conflict_do_update(
                index_elements=index_elements, set_={"origin": TopicOrigin.SOURCE.value}
            )
        )
    else:
        # Same fallback as `_insert_ignore`, and the same reasoning: no
        # supported deployment reaches this branch.
        _insert_ignore(session, FeedItemTopic, rows, index_elements=index_elements)
        return
    session.execute(statement)


def _rule_topic_rows(
    session: Session, item_rows: Sequence[tuple[int, str]]
) -> list[dict[str, object]]:
    """Topic links this batch's own URLs argue for. See ``topicrules``.

    One query for the slug-to-id mapping rather than one per item: the
    ruleset emits a closed vocabulary of about a dozen slugs whatever the
    batch size.

    A slug no ``topics`` row answers to is logged and skipped rather than
    raised. ``tests/cli/test_catalogue.py`` holds the ruleset and the seed
    catalogue in agreement, so the only way here is an instance whose
    operator deleted a topic — which must not stop that source's refresh.
    """
    derived: dict[int, tuple[str, ...]] = {}
    slugs: set[str] = set()
    for item_id, url in item_rows:
        topics = topics_for_url(url)
        if topics:
            derived[item_id] = topics
            slugs.update(topics)
    if not slugs:
        return []

    known = dict(
        session.execute(select(Topic.slug, Topic.id).where(Topic.slug.in_(sorted(slugs))))
        .tuples()
        .all()
    )
    missing = slugs - known.keys()
    if missing:
        log.warning("rule_topic_not_in_catalogue", slugs=sorted(missing))
    return [
        {"feed_item_id": item_id, "topic_id": known[slug], "origin": TopicOrigin.RULE}
        for item_id, topics in derived.items()
        for slug in topics
        if slug in known
    ]


def insert_rule_links(session: Session, pairs: Sequence[tuple[int, int]]) -> None:
    """Write ``(feed_item_id, topic_id)`` pairs as rule-owned links.

    Conflict-ignore, and for the reason every other write in this module
    is: the caller is ``sre-tab retag``, which runs from an operator's
    shell with no share in the scheduler's per-source advisory locks. A
    refresh that lands between the re-tag's snapshot of the link table and
    this write can insert the very same pair — ingest derives it from the
    same URL with the same rules — and a plain insert would then abort the
    whole pass on the primary key for a row that is already correct.

    A pair the refresh has meanwhile promoted to ``origin='source'`` is
    left as the source's, which is the precedence ``_link_source_topics``
    establishes.
    """
    rows: list[dict[str, object]] = [
        {"feed_item_id": item_id, "topic_id": topic_id, "origin": TopicOrigin.RULE}
        for item_id, topic_id in pairs
    ]
    for start in range(0, len(rows), CHUNK_SIZE):
        _insert_ignore(
            session,
            FeedItemTopic,
            rows[start : start + CHUNK_SIZE],
            index_elements=["feed_item_id", "topic_id"],
        )


def upsert_items(
    session: Session,
    *,
    source_id: int,
    items: Sequence[NormalisedItem],
    topic_ids: Sequence[int],
    fetched_at: datetime,
) -> int:
    """Insert items that are new; leave existing ones untouched.

    Returns the number of rows inserted. Topic links are (re-)asserted
    for every item in the batch, existing ones included, so a source
    that gains a topic picks it up without rewriting item rows.

    Two kinds of link are written, in this order and for that reason.
    The source's topics land first as ``origin='source'``, then the ones
    :mod:`app.ingest.topicrules` derives from each item's own URL land as
    ``origin='rule'`` — additive, never replacing, and losing to the
    source's row wherever both name the same pair.
    """
    if not items:
        return 0

    urls = [item.canonical_url for item in items]
    existing: set[str] = set()
    for chunk in _chunks(urls):
        existing.update(
            session.scalars(select(FeedItem.canonical_url).where(FeedItem.canonical_url.in_(chunk)))
        )

    new_rows: list[dict[str, object]] = [
        {
            "source_id": source_id,
            "canonical_url": item.canonical_url,
            "title": item.title,
            "summary": item.summary,
            "published_at": item.published_at,
            "image_url": item.image_url,
            "fetched_at": fetched_at,
            # New rows only, like everything else here: an existing row is
            # never rewritten, and `sre-tab detect-languages` is the pass
            # that brings stored items into line with the detector.
            "language": detect_language(item.title, item.summary),
        }
        for item in items
        if item.canonical_url not in existing
    ]
    _insert_ignore(session, FeedItem, new_rows, index_elements=["canonical_url"])

    # Ids and URLs together, where this used to take ids alone: the rule
    # links below are derived from the URL, and reading both back on the
    # one pass the source links already needed keeps this at one query
    # rather than two.
    item_rows: list[tuple[int, str]] = []
    for chunk in _chunks(urls):
        item_rows.extend(
            session.execute(
                select(FeedItem.id, FeedItem.canonical_url).where(FeedItem.canonical_url.in_(chunk))
            )
            .tuples()
            .all()
        )

    if topic_ids:
        _link_source_topics(
            session,
            [
                {
                    "feed_item_id": item_id,
                    "topic_id": topic_id,
                    "origin": TopicOrigin.SOURCE,
                }
                for item_id, _ in item_rows
                for topic_id in topic_ids
            ],
        )

    _insert_ignore(
        session,
        FeedItemTopic,
        _rule_topic_rows(session, item_rows),
        index_elements=["feed_item_id", "topic_id"],
    )

    session.flush()
    inserted = len(new_rows)
    log.debug(
        "feed_items_stored",
        source_id=source_id,
        candidates=len(items),
        inserted=inserted,
        already_present=len(items) - inserted,
    )
    return inserted


def record_discovered_icon(session: Session, *, source_id: int, icon_url: str | None) -> bool:
    """Store the channel artwork this refresh found. Returns whether it moved.

    Written here rather than through :mod:`app.ingest.status`, which is
    best-effort by design and would drop this as readily as it drops a
    timestamp. This rides the item write instead: the same session, the
    same commit, so a source either records what it fetched or records
    nothing.

    An upsert, because ``source_status`` may not exist yet — a source
    polled for the first time writes its items before the status registry
    writes its row.

    ``None`` is not a value here, it is an absence. A feed that has
    stopped declaring artwork, or a parse that could not make its URL
    safe, leaves the last good icon in place rather than blanking the
    card: a missing ``<image>`` element is far more often a fetch that
    landed on a partial document than a publisher retiring their logo.
    """
    if icon_url is None:
        return False

    existing = session.get(SourceStatus, source_id)
    if existing is not None:
        if existing.discovered_icon_url == icon_url:
            return False
        existing.discovered_icon_url = icon_url
        session.flush()
        return True

    _insert_ignore(
        session,
        SourceStatus,
        [{"source_id": source_id, "discovered_icon_url": icon_url}],
        index_elements=["source_id"],
    )
    session.flush()
    return True


def prune_feed_items(session: Session, *, cutoff: datetime) -> int:
    """Delete items published before *cutoff*, except bookmarked ones.

    ``ON DELETE CASCADE`` still takes an item's topic links and read
    marks with it. Bookmarks are the exception, and deliberately so: a
    bookmark is an explicit "keep this", and evaporating on a retention
    schedule the user never set is the surprising behaviour. So any feed
    item carrying a bookmark row is exempt from retention outright.
    Bookmarked items therefore grow without bound; at 100 users and 25
    sources that is cheaper than losing a saved item.

    The alternative — copying title and URL onto the bookmark row and
    letting the item go — is rejected: a denormalised bookmark can drift
    from the item it names, and a saved link that no longer matches its
    own title is worse than one that costs a row.
    """
    # Correlated EXISTS rather than a join or an IN: it short-circuits on
    # the first bookmark and needs no DISTINCT, on both engines.
    bookmarked = select(Bookmark.feed_item_id).where(Bookmark.feed_item_id == FeedItem.id).exists()

    # synchronize_session=False: this is a bulk delete, and the ORM's
    # in-Python evaluation of the criterion would compare an aware cutoff
    # against SQLite's naive column values.
    statement = (
        delete(FeedItem)
        .where(FeedItem.published_at < cutoff, ~bookmarked)
        .execution_options(synchronize_session=False)
    )
    result = cast("CursorResult[Any]", session.execute(statement))
    removed = result.rowcount or 0
    if removed:
        log.info("feed_items_pruned", removed=removed, cutoff=cutoff.isoformat())
    return removed
