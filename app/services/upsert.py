"""Idempotent inserts against the composite primary keys.

``bookmarks``, ``user_read_items``, and the two preference join tables all
carry composite primary keys. That constraint — not a read-then-write
check in Python — is what makes a repeated client request safe: a check
followed by an insert is two statements with a window between them, and
under concurrency the window is exactly where the duplicate lands.

``INSERT ... ON CONFLICT DO NOTHING`` collapses both into one statement
the database resolves atomically. It is dialect-specific syntax, and the
two dialects in scope are the two the PRD names.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session


def insert_ignore(db: Session, entity: type[Any], rows: Sequence[Mapping[str, Any]]) -> None:
    """Insert ``rows`` into ``entity``'s table, skipping ones that already
    exist. Participates in the caller's transaction; never commits."""
    if not rows:
        return

    dialect = db.get_bind().dialect.name
    values = list(rows)
    if dialect == "postgresql":
        db.execute(pg_insert(entity).values(values).on_conflict_do_nothing())
    elif dialect == "sqlite":
        db.execute(sqlite_insert(entity).values(values).on_conflict_do_nothing())
    else:  # pragma: no cover - PRD scopes the service to these two engines
        raise RuntimeError(f"insert_ignore has no implementation for dialect {dialect!r}")
