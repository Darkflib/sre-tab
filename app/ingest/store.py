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

from app.db.models import Base, Bookmark, FeedItem, FeedItemTopic, SourceStatus
from app.ingest.normalise import NormalisedItem

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
        }
        for item in items
        if item.canonical_url not in existing
    ]
    _insert_ignore(session, FeedItem, new_rows, index_elements=["canonical_url"])

    if topic_ids:
        item_ids: list[int] = []
        for chunk in _chunks(urls):
            item_ids.extend(
                session.scalars(select(FeedItem.id).where(FeedItem.canonical_url.in_(chunk)))
            )
        topic_rows: list[dict[str, object]] = [
            {"feed_item_id": item_id, "topic_id": topic_id}
            for item_id in item_ids
            for topic_id in topic_ids
        ]
        _insert_ignore(
            session, FeedItemTopic, topic_rows, index_elements=["feed_item_id", "topic_id"]
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
