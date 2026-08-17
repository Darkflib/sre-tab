"""Bookmarks: idempotent create, forgiving delete, paginated listing.

Same discipline as read state — the ``(user_id, feed_item_id)`` primary
key carries idempotency, and every statement pins ``user_id`` to the
caller, so a guessed item id gets a user nowhere near another user's
bookmarks.

Unlike the feed, the listing does not require the source to still be
enabled: an operator retiring a source should not silently empty someone's
saved items.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Select, and_, delete, select, tuple_
from sqlalchemy.orm import Session, contains_eager, selectinload

from app.api.v1.schemas import BookmarkOut, BookmarkPage
from app.db.models import Bookmark, FeedItem, User, UserReadItem
from app.services.errors import ItemNotFoundError
from app.services.feed import build_item_out
from app.services.pagination import as_utc, decode_cursor, encode_cursor
from app.services.upsert import insert_ignore


def create_bookmark(db: Session, user: User, item_id: int) -> BookmarkOut:
    """Bookmark an item and commit. Repeats return the existing bookmark
    with its original ``created_at`` — a second click is not a new save."""
    # Existence first: feed_item_id is a foreign key, so inserting against
    # an unknown item would surface as an IntegrityError — a 500 dressed
    # up as a database fault rather than the 404 it is.
    if db.scalar(select(FeedItem.id).where(FeedItem.id == item_id)) is None:
        raise ItemNotFoundError(f"no feed item {item_id}")

    # ``created_at`` is supplied here rather than left to the column's
    # ``server_default=func.now()`` on purpose. It is half the pagination
    # key, and SQLite's CURRENT_TIMESTAMP renders whole seconds with no
    # fractional part while a bound datetime renders six digits. SQLite
    # compares those as text, so "…:04" sorts before "…:04.000000" and a
    # keyset page never advances past a server-defaulted row. Writing the
    # value through the type's bind processor makes every row's stored
    # form identical, on both engines. ON CONFLICT DO NOTHING means a
    # repeat call keeps the original timestamp.
    insert_ignore(
        db,
        Bookmark,
        [{"user_id": user.id, "feed_item_id": item_id, "created_at": datetime.now(UTC)}],
    )
    db.flush()

    item, created_at, read_at = db.execute(_card_query(user).where(FeedItem.id == item_id)).one()
    db.commit()
    return BookmarkOut(
        item=build_item_out(item, read=read_at is not None, bookmarked=True),
        created_at=as_utc(created_at),
    )


def remove_bookmark(db: Session, user: User, item_id: int) -> None:
    """Remove this user's bookmark and commit.

    Removing one that is not there is a no-op, not an error: the client
    retrying a delete it already made should converge, not fail.
    """
    if db.scalar(select(FeedItem.id).where(FeedItem.id == item_id)) is None:
        raise ItemNotFoundError(f"no feed item {item_id}")

    db.execute(
        delete(Bookmark).where(Bookmark.user_id == user.id, Bookmark.feed_item_id == item_id)
    )
    db.commit()


def list_bookmarks(
    db: Session, user: User, *, cursor: str | None = None, limit: int = 25
) -> BookmarkPage:
    """This user's bookmarks, most recently saved first.

    Keyset paginated on ``(created_at, feed_item_id)`` — the same shape as
    the feed, so a tie in ``created_at`` (a burst of saves inside one
    clock tick) still has a total order and no page can repeat or skip.
    """
    statement = _card_query(user).order_by(Bookmark.created_at.desc(), Bookmark.feed_item_id.desc())
    if cursor is not None:
        statement = statement.where(
            tuple_(Bookmark.created_at, Bookmark.feed_item_id) < decode_cursor(cursor)
        )

    rows = db.execute(statement.limit(limit + 1)).all()
    has_more = len(rows) > limit
    page = rows[:limit]

    bookmarks = [
        BookmarkOut(
            item=build_item_out(item, read=read_at is not None, bookmarked=True),
            created_at=as_utc(created_at),
        )
        for item, created_at, read_at in page
    ]
    next_cursor = encode_cursor(page[-1][1], page[-1][0].id) if has_more else None
    return BookmarkPage(bookmarks=bookmarks, next_cursor=next_cursor)


def _card_query(user: User) -> Select[tuple[FeedItem, datetime, datetime]]:
    """Bookmarked items for one user, with everything a card needs.

    The bookmark join is inner and pinned to this user, so it doubles as
    the ownership filter; ``source`` and ``topics`` are loaded eagerly
    because both are ``lazy="raise"`` and rendering a card touches both.
    """
    return (
        select(FeedItem, Bookmark.created_at, UserReadItem.read_at)
        .join(FeedItem.source)
        .join(
            Bookmark,
            and_(Bookmark.feed_item_id == FeedItem.id, Bookmark.user_id == user.id),
        )
        .outerjoin(
            UserReadItem,
            and_(
                UserReadItem.feed_item_id == FeedItem.id,
                UserReadItem.user_id == user.id,
            ),
        )
        .options(contains_eager(FeedItem.source), selectinload(FeedItem.topics))
    )
