"""Feed reads: keyset pagination, filtering, and per-user state folding.

Two properties drive the shape of the query.

*Keyset, not OFFSET.* ``feed_items`` carries a composite
``(published_at, id)`` index for exactly this: a page is an index seek to
the cursor position plus a scan of ``limit + 1`` rows, at any depth.
``OFFSET`` would make page cost grow with page number and put the p95
target at the mercy of how far a user has scrolled.

*One query, not one per card.* ``FeedItemOut`` folds the per-user ``read``
and ``bookmarked`` flags in so agent D renders a card from a single call.
The naive way to produce them is a lookup per item, which is the classic
N+1 — and with ``lazy="raise"`` on every relationship, the equivalent slip
on ``source`` or ``topics`` raises rather than silently working. So the
state tables are LEFT JOINed on the composite key with ``user_id`` pinned
to the caller (at most one row each, so no row multiplication under
LIMIT), ``source`` rides the join it already needs via ``contains_eager``,
and ``topics`` is a single ``selectinload`` over the page's ids. Two
statements per page, independent of page size.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, and_, select, tuple_
from sqlalchemy.orm import Session, contains_eager, selectinload

from app.api.v1.schemas import FeedItemOut, FeedPage, FeedSourceRef
from app.db.models import (
    Bookmark,
    FeedItem,
    FeedItemTopic,
    Source,
    Topic,
    User,
    UserPreferenceTopic,
    UserReadItem,
)
from app.services.pagination import as_utc, decode_cursor, encode_cursor
from app.services.preferences import selected_source_slugs


def get_feed_page(
    db: Session,
    user: User,
    *,
    topics: Sequence[str] | None = None,
    sources: Sequence[str] | None = None,
    cursor: str | None = None,
    limit: int = 25,
) -> FeedPage:
    """One page of the feed, newest first.

    ``topics``/``sources`` absent means "use the caller's saved
    selection"; present means "narrow to exactly these", including the
    case where nothing matches. Raises
    :class:`app.services.errors.InvalidCursorError` for a cursor we did
    not issue.
    """
    statement = _base_query(user)
    statement = _apply_filters(db, statement, user, topics, sources)

    if cursor is not None:
        position = decode_cursor(cursor)
        statement = statement.where(tuple_(FeedItem.published_at, FeedItem.id) < position)

    # One row over the page size is the exhaustion probe: it tells us
    # whether to mint a cursor without a second COUNT query.
    rows = db.execute(statement.limit(limit + 1)).all()
    has_more = len(rows) > limit
    page = rows[:limit]

    items = [
        build_item_out(item, read=read_at is not None, bookmarked=bookmarked_at is not None)
        for item, read_at, bookmarked_at in page
    ]
    next_cursor = encode_cursor(page[-1][0].published_at, page[-1][0].id) if has_more else None
    return FeedPage(items=items, next_cursor=next_cursor)


def _base_query(user: User) -> Select[tuple[FeedItem, datetime, datetime]]:
    return (
        select(FeedItem, UserReadItem.read_at, Bookmark.created_at)
        .join(FeedItem.source)
        .outerjoin(
            UserReadItem,
            and_(
                UserReadItem.feed_item_id == FeedItem.id,
                # Pinning user_id inside the ON clause, not the WHERE, is
                # what keeps this an outer join: in the WHERE it would
                # silently become an inner join and drop unread items.
                UserReadItem.user_id == user.id,
            ),
        )
        .outerjoin(
            Bookmark,
            and_(Bookmark.feed_item_id == FeedItem.id, Bookmark.user_id == user.id),
        )
        # Items from a disabled source stay in the database (ingest never
        # deletes on failure) but leave the feed.
        .where(Source.enabled.is_(True))
        .options(contains_eager(FeedItem.source), selectinload(FeedItem.topics))
        .order_by(FeedItem.published_at.desc(), FeedItem.id.desc())
    )


def _apply_filters(
    db: Session,
    statement: Select[tuple[FeedItem, datetime, datetime]],
    user: User,
    topics: Sequence[str] | None,
    sources: Sequence[str] | None,
) -> Select[tuple[FeedItem, datetime, datetime]]:
    source_filter = _effective_sources(db, user, sources)
    if source_filter is not None:
        statement = statement.where(Source.slug.in_(source_filter))

    topic_filter = _effective_topics(db, user, topics)
    if topic_filter is not None:
        statement = statement.where(
            FeedItem.id.in_(
                select(FeedItemTopic.feed_item_id)
                .join(Topic, Topic.id == FeedItemTopic.topic_id)
                .where(Topic.slug.in_(topic_filter))
            )
        )
    return statement


def _effective_sources(db: Session, user: User, requested: Sequence[str] | None) -> set[str] | None:
    """Source slugs to narrow to, or ``None`` for no narrowing.

    An explicit request is honoured verbatim — an unknown slug narrows to
    nothing and returns an empty page, which is the honest answer to a
    filter nothing satisfies. Falling back to a saved selection that is
    empty means the opposite: the PRD's "a user with no selection sees
    the instance defaults".
    """
    if requested is not None:
        return set(requested)
    selected = set(db.scalars(selected_source_slugs(user.id)).all())
    return selected or None


def _effective_topics(db: Session, user: User, requested: Sequence[str] | None) -> set[str] | None:
    if requested is not None:
        return set(requested)

    # One query for "which enabled topics exist" and "which did this user
    # select": the count comparison below needs both.
    rows = db.execute(
        select(Topic.slug, UserPreferenceTopic.user_id)
        .outerjoin(
            UserPreferenceTopic,
            and_(
                UserPreferenceTopic.topic_id == Topic.id,
                UserPreferenceTopic.user_id == user.id,
            ),
        )
        .where(Topic.enabled.is_(True))
    ).all()
    selected = {slug for slug, owner in rows if owner is not None}

    # A selection covering every enabled topic narrows nothing, so skip
    # the join. Not just an optimisation: the topic predicate is "carries
    # one of these topics", which also excludes items carrying none, and
    # an as-yet-unclassified item disappearing from a source the user
    # explicitly enabled would read as data loss rather than as a filter.
    if not selected or len(selected) == len(rows):
        return None
    return selected


def build_item_out(item: FeedItem, *, read: bool, bookmarked: bool) -> FeedItemOut:
    """Build the response card. ``item.source`` and ``item.topics`` must
    already be loaded — ``lazy="raise"`` turns a miss here into a loud
    failure rather than a per-card query."""
    return FeedItemOut(
        id=item.id,
        canonical_url=item.canonical_url,
        title=item.title,
        summary=item.summary,
        image_url=item.image_url,
        published_at=as_utc(item.published_at),
        source=FeedSourceRef(
            slug=item.source.slug, name=item.source.name, icon_url=item.source.icon_url
        ),
        topics=sorted(topic.slug for topic in item.topics),
        read=read,
        bookmarked=bookmarked,
    )
