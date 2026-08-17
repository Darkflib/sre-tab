"""Mutual exclusion between replicas.

**PostgreSQL.** Session-level advisory locks. Each named lock gets a
stable 64-bit key from a BLAKE2b digest of its name, and is held on a
dedicated connection for the duration of the block. Session-level rather
than transaction-level because a transaction-scoped lock would be
released by any rollback inside the job; and released explicitly in a
``finally`` because the connection returns to the pool rather than
closing, and a pooled connection carries its advisory locks with it.

Locks are taken **per source**, not once globally. The requirement is
that two replicas never fetch the same source at the same time, not that
one replica does all the work, so ``sre-tab:source:<id>`` lets replicas
share the tick while still colliding on nothing. The prune job takes one
global lock, since running it twice is pointless rather than harmful.

**SQLite.** There is no advisory-lock equivalent, and pretending
otherwise would be worse than admitting it. :class:`SingleProcessLock`
excludes only within one process. On SQLite the deployment is therefore
single-process by assumption — which matches the PRD, where SQLite is
for local development and PostgreSQL is production. A warning is logged
once at start-up so this is never a silent assumption.
"""

from __future__ import annotations

import hashlib
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager

import structlog
from sqlalchemy import Engine, text

log = structlog.get_logger("app.scheduler.locks")

SOURCE_LOCK_PREFIX = "sre-tab:source:"
PRUNE_LOCK_NAME = "sre-tab:prune"


def advisory_key(name: str) -> int:
    """Stable signed 64-bit key for a lock name."""
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def source_lock_name(source_id: int) -> str:
    return f"{SOURCE_LOCK_PREFIX}{source_id}"


class LeaderLock(ABC):
    """Try-acquire semantics: never blocks, yields whether it won."""

    #: Reported by the readiness probe so an operator can see which
    #: strategy is actually in force.
    kind: str

    @abstractmethod
    def acquire(self, name: str) -> AbstractContextManager[bool]:
        """Context manager yielding ``True`` when the lock was taken."""


class PostgresAdvisoryLock(LeaderLock):
    kind = "postgres-advisory"

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def acquire(self, name: str) -> AbstractContextManager[bool]:
        return self._acquire(name)

    @contextmanager
    def _acquire(self, name: str) -> Iterator[bool]:
        key = advisory_key(name)
        with self._engine.connect() as connection:
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
                ).scalar_one()
            )
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                    connection.commit()


class SingleProcessLock(LeaderLock):
    """In-process only. See the module docstring: this is the documented
    SQLite degradation, not an advisory-lock substitute."""

    kind = "single-process"

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def acquire(self, name: str) -> AbstractContextManager[bool]:
        return self._acquire(name)

    @contextmanager
    def _acquire(self, name: str) -> Iterator[bool]:
        with self._guard:
            lock = self._locks.setdefault(name, threading.Lock())
        acquired = lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()


def build_leader_lock(engine: Engine) -> LeaderLock:
    """Pick a strategy from the engine's dialect, loudly."""
    if engine.dialect.name == "postgresql":
        return PostgresAdvisoryLock(engine)
    log.warning(
        "scheduler_lock_degraded",
        dialect=engine.dialect.name,
        detail=(
            "no advisory locks on this engine; scheduling assumes a single "
            "process. Run PostgreSQL before adding web replicas."
        ),
    )
    return SingleProcessLock()
