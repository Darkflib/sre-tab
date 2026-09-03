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

*Muting is that predicate negated, and it is a filter rather than an
index seek.* A GIN index answers "which rows match"; nothing indexes
"which rows do not", so the mute predicate is evaluated per row. What
bounds it is the ordering: the scan walks ``(published_at, id)``
descending and stops once it has ``limit + 1`` rows that survive every
filter, so the cost is a page's worth of rows plus whatever a reader's
own mutes push past — not the corpus.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, func, literal_column, or_, select, tuple_
from sqlalchemy.dialects.postgresql import TSQUERY, TSVECTOR
from sqlalchemy.orm import Session, contains_eager, selectinload

from app.api.v1.schemas import FeedItemOut, FeedPage, FeedSourceRef
from app.api.v1.schemas.feed import ReadFilter
from app.db.models import (
    Bookmark,
    FeedItem,
    FeedItemTopic,
    MuteKind,
    Source,
    Topic,
    User,
    UserMutedTerm,
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

    for predicate in mute_predicates(db, user):
        statement = statement.where(predicate)

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


#: Bound on the terms taken from one search query. Past this the extra
#: words narrow nothing a reader would notice and each one is another
#: clause; on SQLite that is another ``LIKE`` over the same scan.
MAX_SEARCH_TERMS = 8

#: The text both search and muting are matched against, spelled once. The
#: PostgreSQL index in revision ``b7c1e0a94f6d`` declares this same
#: expression, and the planner only uses an expression index when the two
#: match exactly — so this constant and that migration are a pair, and the
#: PostgreSQL suite asserts the plan rather than trusting the resemblance.
SEARCH_DOCUMENT_SQL = "feed_items.title || ' ' || coalesce(feed_items.summary, '')"

#: The text-search configuration, as a literal rather than the session's
#: ``default_text_search_config``: ``to_tsvector(text)`` is STABLE and
#: ``to_tsvector(regconfig, text)`` is IMMUTABLE, and only an IMMUTABLE
#: expression can be indexed.
_ENGLISH: ColumnElement[str] = literal_column("'english'")


def _text_match(db: Session, terms: Sequence[str], *, require_all: bool) -> ColumnElement[bool]:
    """True where the item's text matches *terms* — all of them, or any.

    One function for both readers of it. Search asserts it over the words
    of a query, with ``require_all``; muting negates it over a reader's
    muted terms, without. Writing it once is why search landed first: the
    alternative was two copies that had to agree about stemming, case
    folding, and what a word boundary is, on two engines.

    Two engines, because they are not interchangeable here and pretending
    otherwise would hide the difference rather than remove it.

    *PostgreSQL* matches a ``tsquery`` against a ``to_tsvector`` of title
    and summary. Each term becomes its own ``plainto_tsquery``, combined
    with ``&&`` or ``||``. ``plainto_tsquery`` rather than ``to_tsquery``
    because it takes a reader's words as words — no operator syntax to get
    wrong, and no malformed-input branch — and rather than
    ``websearch_to_tsquery`` because that one's quoted phrases and ``-``
    exclusions have no honest counterpart below, and an engine divergence
    in *semantics* is worse than one in recall.

    Per term rather than one query over the joined string, and that
    matters on the muting side: a muted phrase stays an AND of its own
    words, so "premier league" hides the league rather than every item
    mentioning a premier. It also keeps the whole thing a single ``@@``
    against a single vector, so forty muted words cost one match.

    *SQLite* uses a case-folded substring per term. This is the
    development engine (``app/db/engine.py``), and the roadmap entry that
    specified search sanctioned ``LIKE`` for it. The divergence is real
    and is stated rather than papered over. PostgreSQL matches stemmed
    words: ``bookmarks`` finds a title saying "bookmark", and ``cat``
    finds nothing that only says "catalogue". SQLite matches substrings,
    so ``cat`` finds the catalogue — and a mute is where that costs most,
    because a short muted word quietly removes more than it was meant to.
    Worth knowing before reading a laptop's feed as evidence about
    production.

    ``autoescape`` is not decoration: ``%`` and ``_`` are wildcards, so a
    reader searching for ``100%`` would otherwise match everything, and
    muting ``c_t`` would hide every item with "cat", "cut", or "cot".
    """
    if db.get_bind().dialect.name == "postgresql":
        # Built from expressions rather than from `text()`, and that is not
        # style. A `TextClause` cannot be negated — `~` on one trips an
        # assertion inside SQLAlchemy rather than producing `NOT (…)` — and
        # muting is this predicate negated, so the raw-SQL version worked
        # for search and broke the moment it had a second caller.
        #
        # `literal_column` is what keeps the rendered SQL identical to the
        # index expression in revision `b7c1e0a94f6d`, which the plan
        # assertion in `tests/postgres/test_search.py` holds it to. The
        # terms stay bound parameters.
        document = func.to_tsvector(_ENGLISH, literal_column(SEARCH_DOCUMENT_SQL), type_=TSVECTOR)
        combine = "&&" if require_all else "||"
        query: ColumnElement[Any] = func.plainto_tsquery(_ENGLISH, terms[0], type_=TSQUERY)
        for term in terms[1:]:
            query = query.op(combine, return_type=TSQUERY)(
                func.plainto_tsquery(_ENGLISH, term, type_=TSQUERY)
            )
        return document.bool_op("@@")(query)

    document_text = func.lower(FeedItem.title + " " + func.coalesce(FeedItem.summary, ""))
    clauses = [document_text.contains(term.lower(), autoescape=True) for term in terms]
    return and_(*clauses) if require_all else or_(*clauses)


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

    Every word required, which is what a reader typing two of them means.
    """
    terms = search_terms(query)
    if not terms:
        return None
    return _text_match(db, terms, require_all=True)


def mute_predicates(db: Session, user: User) -> list[ColumnElement[bool]]:
    """Predicates excluding what this user has muted; ``[]`` for nobody.

    Two of them rather than one, because the two kinds match different
    things — words against the item's text, tags against its topic links —
    and folding them into a single ``OR`` would put an unrelated subquery
    inside the text predicate for no gain.

    One statement, not two: both kinds come back together, which is the
    shape ``_effective_topics`` already uses and for the same reason.

    **Bookmarks are deliberately not filtered.** ``app.services.bookmarks``
    does not call this, and a bookmark is an explicit "keep this" — the
    argument ``prune_feed_items`` already makes for exempting bookmarks
    from retention. A saved item vanishing because a word was muted a
    month later is the same surprise in a quieter form.
    """
    rows = db.execute(
        select(UserMutedTerm.kind, UserMutedTerm.term).where(UserMutedTerm.user_id == user.id)
    ).all()
    if not rows:
        return []

    words = sorted({term for kind, term in rows if kind is MuteKind.WORD})
    tags = {term for kind, term in rows if kind is MuteKind.TAG}

    predicates: list[ColumnElement[bool]] = []
    if words:
        predicates.append(~_text_match(db, words, require_all=False))
    if tags:
        # The topic filter's subquery, negated. `feed_item_topics.feed_item_id`
        # is NOT NULL, so `NOT IN` cannot go three-valued here — the trap
        # that makes `NOT IN` over a nullable column return nothing at all.
        predicates.append(
            ~FeedItem.id.in_(
                select(FeedItemTopic.feed_item_id)
                .join(Topic, Topic.id == FeedItemTopic.topic_id)
                .where(Topic.slug.in_(tags))
            )
        )
    return predicates


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
