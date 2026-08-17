"""Opaque keyset cursors for time-ordered listings.

Both paginated listings order by ``(timestamp DESC, id DESC)`` and page
with a keyset predicate rather than ``OFFSET``: ``feed_items`` carries the
composite ``(published_at, id)`` index precisely so a page costs an index
seek plus a bounded scan, whatever the page depth. ``OFFSET`` degrades
linearly and the p95 target does not survive it.

The ``id`` half of the key is what makes the cursor stable across ties.
Publication timestamps collide routinely — a source that stamps whole
seconds, or a batch import that lands several items with the same value —
and an ordering keyed on the timestamp alone has no defined position
inside a tie, so rows can repeat or vanish between pages.

The encoding is base64url over ``<version>:<microseconds>:<id>``. It is
opaque, not authenticated: a forged cursor can only move the window
within a listing the caller is already entitled to read, and every query
is separately scoped to the current user, so a signature would buy
nothing. Decoding is total — anything unparseable raises
:class:`InvalidCursorError`, which routes answer with 400.
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime, timedelta

from app.services.errors import InvalidCursorError

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
_VERSION = "1"


def as_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime.

    SQLite has no timestamp type, so ``DateTime(timezone=True)`` columns
    come back naive there while PostgreSQL returns them aware. Everything
    is written as UTC, so attaching UTC to a naive value is a restoration,
    not a guess — and it keeps cursors and API output identical on both
    engines.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def encode_cursor(when: datetime, row_id: int) -> str:
    """Encode one keyset position. Microseconds keep the round trip exact
    on both engines; SQLite stores six fractional digits."""
    micros = (as_utc(when) - _EPOCH) // _MICROSECOND
    payload = f"{_VERSION}:{micros}:{row_id}"
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    """Decode a cursor into ``(timestamp, id)``.

    Raises :class:`InvalidCursorError` for anything we did not issue.
    """
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = base64.urlsafe_b64decode(cursor + padding).decode()
        version, micros, row_id = payload.split(":")
        position = (_EPOCH + int(micros) * _MICROSECOND, int(row_id))
    # OverflowError is in the tuple because `int(micros)` is unbounded
    # while timedelta is not: a few hundred digits of nines parses fine
    # and then overflows the multiplication. Decoding is total, so that
    # is a malformed cursor like any other, not a 500.
    except (ValueError, binascii.Error, UnicodeDecodeError, OverflowError) as exc:
        raise InvalidCursorError("malformed cursor") from exc
    if version != _VERSION:
        raise InvalidCursorError("unrecognised cursor version")
    return position
