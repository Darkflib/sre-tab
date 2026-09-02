"""Idempotent inserts against the composite primary keys.

``bookmarks``, ``user_read_items``, and the two preference join tables all
carry composite primary keys. That constraint — not a read-then-write
check in Python — is what makes a repeated client request safe: a check
followed by an insert is two statements with a window between them, and
under concurrency the window is exactly where the duplicate lands.

``INSERT ... ON CONFLICT DO NOTHING`` collapses both into one statement
the database resolves atomically. It is dialect-specific syntax, and the
two dialects in scope are the two the PRD names.

``upsert_returning`` is the same idea with ``DO UPDATE ... RETURNING``,
for the shape DO NOTHING serves badly: a row the caller needs *back*,
carrying mutable fields to refresh. Three things separate them, and only
the first is about concurrency.

**The obvious rejection of DO NOTHING is wrong, and it was measured
rather than reasoned about.** The expectation was that DO NOTHING takes
no lock on the conflicting row, so a caller racing an uncommitted insert
would affect zero rows, fail to see the other transaction's row in the
follow-up ``SELECT``, and fall out holding ``None``. Against PostgreSQL
18 that is not what happens: speculative insertion waits on the
conflicting transaction, and the ``SELECT`` — a separate statement, and
so a fresh snapshot — then reads the committed row. The pairing works.
It works *because the connection is at READ COMMITTED*, which nothing in
``create_db_engine`` sets and no test asserts; under REPEATABLE READ the
same pair raises a serialization failure instead. DO UPDATE returns the
surviving row from the statement that resolved the conflict, so there is
no second snapshot for its correctness to rest on.

Second, DO NOTHING followed by a re-select has a ``None`` branch that
cannot be reached and therefore cannot be tested, and whose only correct
handling is a retry loop whose termination depends on another
transaction committing. RETURNING has no such branch.

Third, and the plain one: DO UPDATE has somewhere to put the refresh, so
a create path and an update path become one statement instead of two
branches that have to be kept in step.

None of which retires ``insert_ignore``. A pure join table has nothing to
update and nothing to read back, and DO NOTHING is exactly right for it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from sqlalchemy import Executable
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.models import Base


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


def upsert_returning[EntityT: Base](
    db: Session,
    entity: type[EntityT],
    values: Mapping[str, Any],
    *,
    index_elements: Sequence[str],
    update: Mapping[str, Any],
) -> EntityT:
    """Insert ``values``; on a conflict against ``index_elements``, apply
    ``update`` to the row already there. Returns the surviving row either
    way, as a live ORM instance. Participates in the caller's
    transaction; never commits.

    ``update`` has to name every column that should move, ``updated_at``
    included. A column's ``onupdate`` is an ORM-flush hook: SQLAlchemy
    does not fold it into a hand-written DO UPDATE set clause, so a
    timestamp left out here silently stops advancing rather than
    failing.
    """
    dialect = db.get_bind().dialect.name
    keys = list(index_elements)
    row = dict(values)
    changes = dict(update)

    if dialect == "postgresql":
        statement: Executable = (
            pg_insert(entity)
            .values(row)
            .on_conflict_do_update(index_elements=keys, set_=changes)
            .returning(entity)
        )
    elif dialect == "sqlite":
        statement = (
            sqlite_insert(entity)
            .values(row)
            .on_conflict_do_update(index_elements=keys, set_=changes)
            .returning(entity)
        )
    else:  # pragma: no cover - PRD scopes the service to these two engines
        raise RuntimeError(f"upsert_returning has no implementation for dialect {dialect!r}")

    # populate_existing, because otherwise the identity map wins over the
    # RETURNING row: a second upsert in one session hands back the first
    # one's attributes and says nothing about it.
    found = db.scalars(statement, execution_options={"populate_existing": True}).one()
    return cast(EntityT, found)
