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

*Search is a predicate, not a second endpoint.* ``q`` narrows inside the
same statement, so the ``LIMIT`` is taken after the narrowing and pages
stay full — the argument :func:`_apply_filters` already makes for the
read-state filter. Ordering stays ``published_at`` descending rather than
becoming relevance: the cursor *is* ``(published_at, id)``, so ranking
would need a different key and would invalidate every cursor already
issued, to reorder a corpus a reader is searching by recency anyway.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from sqlalchemy import ColumnElement, Select, and_, func, select, text, tuple_
from sqlalchemy.orm import Session, contains_eager, selectinload

from app.api.v1.schemas import FeedItemOut, FeedPage, FeedSourceRef
from app.api.v1.schemas.feed import ReadFilter
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
    read_state: ReadFilter = ReadFilter.ALL,
    query: str | None = None,
    cursor: str | None = None,
    limit: int = 25,
) -> FeedPage:
    """One page of the feed, newest first.

    ``topics``/``sources`` absent means "use the caller's saved
    selection"; present means "narrow to exactly these", including the
    case where nothing matches. ``read_state`` and ``query`` have no saved
    counterpart — both default to narrowing nothing. Raises
    :class:`app.services.errors.InvalidCursorError` for a cursor we did
    not issue.
    """
    statement = _base_query(user)
    statement = _apply_filters(db, statement, user, topics, sources, read_state, query)

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
    read_state: ReadFilter,
    query: str | None,
) -> Select[tuple[FeedItem, datetime, datetime]]:
    # A predicate over the read join the base query already carries, in
    # the WHERE of the same statement rather than a filter applied to the
    # page afterwards. That is what keeps keyset pagination honest: the
    # LIMIT is taken after the narrowing, so pages stay full and the
    # cursor still names a row that satisfies the filter. Discarding rows
    # in Python would yield short, ragged pages and a cursor pointing at
    # an item the caller never saw.
    if read_state is ReadFilter.UNREAD:
        statement = statement.where(UserReadItem.read_at.is_(None))
    elif read_state is ReadFilter.READ:
        statement = statement.where(UserReadItem.read_at.is_not(None))

    source_filter = _effective_sources(db, user, sources)
    if source_filter is not None:
        statement = statement.where(Source.slug.in_(source_filter))

    match = search_predicate(db, query)
    if match is not None:
        statement = statement.where(match)

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


#: Bound on the terms taken from one query. Past this the extra words
#: narrow nothing a reader would notice and each one is another predicate;
#: on SQLite that is another ``LIKE`` over the same scan.
MAX_SEARCH_TERMS = 8

#: The text a query is matched against, spelled once. The PostgreSQL index
#: in revision ``b7c1e0a94f6d`` declares this same expression, and the
#: planner only uses an expression index when the two match exactly — so
#: this constant and that migration are a pair, and the PostgreSQL suite
#: asserts the plan rather than trusting the resemblance.
SEARCH_DOCUMENT_SQL = "feed_items.title || ' ' || coalesce(feed_items.summary, '')"

_POSTGRES_MATCH = text(
    f"to_tsvector('english', {SEARCH_DOCUMENT_SQL}) @@ plainto_tsquery('english', :search_query)"
)


def search_terms(query: str | None) -> list[str]:
    """The words a query narrows on, or ``[]`` for one that narrows nothing.

    Whitespace-only and absent are the same answer deliberately: a search
    box the reader has cleared must return the unnarrowed feed rather than
    an empty one.
    """
    if query is None:
        return []
    return query.split()[:MAX_SEARCH_TERMS]


def search_predicate(db: Session, query: str | None) -> ColumnElement[bool] | None:
    """Full-text narrowing for *query*, or ``None`` when it narrows nothing.

    Two implementations, because the two engines are not interchangeable
    here and pretending otherwise would hide the difference rather than
    remove it.

    *PostgreSQL* matches ``plainto_tsquery`` against a ``to_tsvector`` of
    title and summary. ``plainto_tsquery`` rather than ``to_tsquery``
    because it takes the reader's words as words — no operator syntax to
    get wrong, and no malformed-input branch — and rather than
    ``websearch_to_tsquery`` because that one's quoted phrases and ``-``
    exclusions have no honest counterpart below, and an engine divergence
    in *semantics* is worse than one in recall. It ANDs the terms, which
    is what a reader typing two words means.

    *SQLite* requires every term as a case-folded substring. This is the
    development engine (``app/db/engine.py``), and the roadmap entry that
    specified this work sanctioned ``LIKE`` for it. The divergence is real
    and is stated rather than papered over. PostgreSQL matches stemmed
    words: ``bookmarks`` finds a title saying "bookmark", and ``cat``
    finds nothing that only says "catalogue". SQLite matches substrings,
    so ``cat`` finds the catalogue. Recall differs between the engine a
    developer runs and the one that serves anybody, which is a thing to
    know before reading a search result on a laptop as evidence.

    ``autoescape`` is not decoration: ``%`` and ``_`` are wildcards, so a
    reader searching for ``100%`` would otherwise match everything.
    """
    terms = search_terms(query)
    if not terms:
        return None

    if db.get_bind().dialect.name == "postgresql":
        # `text()` is a `TextClause`, which is what `.where()` wants and
        # not what it is annotated to want. The cast is the annotation
        # catching up, not a claim about the SQL.
        return cast("ColumnElement[bool]", _POSTGRES_MATCH.bindparams(search_query=" ".join(terms)))

    document = func.lower(FeedItem.title + " " + func.coalesce(FeedItem.summary, ""))
    return and_(*(document.contains(term.lower(), autoescape=True) for term in terms))


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
