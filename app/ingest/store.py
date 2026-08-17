"""Idempotent writes for ingested items — acceptance criterion 2.

Re-fetching a feed must not duplicate items, and must not resurrect or
reorder ones already stored. So the write is insert-or-ignore on
``feed_items.canonical_url``: an item that exists is left exactly as it
is, including its ``published_at``, which is what the feed's ordering
depends on. Nothing here ever updates or deletes an existing row.

The existence check is advisory — it keeps the common re-fetch cheap —
and the ``ON CONFLICT DO NOTHING`` behind it is what makes the write
correct when two replicas race.
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

from app.db.models import Base, FeedItem, FeedItemTopic
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

    session.commit()
    inserted = len(new_rows)
    log.debug(
        "feed_items_stored",
        source_id=source_id,
        candidates=len(items),
        inserted=inserted,
        already_present=len(items) - inserted,
    )
    return inserted


def prune_feed_items(session: Session, *, cutoff: datetime) -> int:
    """Delete items published before *cutoff*. Returns rows removed.

    ``ON DELETE CASCADE`` takes the item's topic links, read marks, and
    bookmarks with it — retention applies to the item, not to whether
    somebody bookmarked it.
    """
    # synchronize_session=False: this is a bulk delete, and the ORM's
    # in-Python evaluation of the criterion would compare an aware cutoff
    # against SQLite's naive column values.
    statement = (
        delete(FeedItem)
        .where(FeedItem.published_at < cutoff)
        .execution_options(synchronize_session=False)
    )
    result = cast("CursorResult[Any]", session.execute(statement))
    session.commit()
    removed = result.rowcount or 0
    if removed:
        log.info("feed_items_pruned", removed=removed, cutoff=cutoff.isoformat())
    return removed
