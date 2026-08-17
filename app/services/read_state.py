"""Per-user read state.

Idempotent in both directions, and idempotent by construction rather than
by checking first: marking read is an ``INSERT ... ON CONFLICT DO
NOTHING`` against the ``(user_id, feed_item_id)`` primary key, marking
unread is a ``DELETE`` that is happy to match nothing. Neither has a
read-then-write window, so two rapid clicks cannot race into an error.

Every statement pins ``user_id`` to the caller. One user's read state is
unreachable from another's session even with a guessed item id — that is
acceptance criterion 4, not an implementation detail.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.v1.schemas import ReadStateOut
from app.db.models import FeedItem, User, UserReadItem
from app.services.errors import ItemNotFoundError
from app.services.pagination import as_utc
from app.services.upsert import insert_ignore


def set_read_state(db: Session, user: User, item_id: int, *, read: bool) -> ReadStateOut:
    """Mark an item read or unread for this user and commit.

    The whole update is one transaction: the session opened by ``get_db``
    is committed here on success, and rolled back by that dependency's
    context manager if anything raises.
    """
    if db.scalar(select(FeedItem.id).where(FeedItem.id == item_id)) is None:
        raise ItemNotFoundError(f"no feed item {item_id}")

    if read:
        # Explicit timestamp, matching bookmarks: SQLite's
        # CURRENT_TIMESTAMP has whole-second resolution, so a
        # server-defaulted value loses the sub-second ordering the client
        # displays and stores in a different textual form from bound
        # datetimes. See the note in app/services/bookmarks.py.
        insert_ignore(
            db,
            UserReadItem,
            [{"user_id": user.id, "feed_item_id": item_id, "read_at": datetime.now(UTC)}],
        )
        # Re-read rather than trusting the insert: on the repeat call the
        # row already existed, and the original read_at is the truthful
        # answer. A marked-read item does not get a fresher timestamp for
        # being marked read twice.
        read_at = db.scalar(
            select(UserReadItem.read_at).where(
                UserReadItem.user_id == user.id, UserReadItem.feed_item_id == item_id
            )
        )
    else:
        db.execute(
            delete(UserReadItem).where(
                UserReadItem.user_id == user.id, UserReadItem.feed_item_id == item_id
            )
        )
        read_at = None

    db.commit()
    return ReadStateOut(
        item_id=item_id, read=read, read_at=as_utc(read_at) if read_at is not None else None
    )
