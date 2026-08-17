"""Per-source refresh outcome, in memory and in the database.

Two consumers with different needs, so two representations:

- The **readiness probe** runs in the process that did the fetching and
  wants full detail — item counts, next-due time, the last error. That
  is :class:`SourceStatusRegistry`, in memory, thread-safe because the
  scheduler writes to it from its own thread.
- The **operator CLI** is a separate process and can see only what was
  written down. That is the ``source_status`` table, one row per source,
  written through on every recorded outcome.

The table is also what keeps the refresh schedule honest across a
restart or a second replica: ``last_fetched_at`` persists, so a cold
process asks the database when a source was last attempted rather than
treating every source as due immediately and re-fetching the whole
catalogue on every deploy.

Persistence is best-effort by design. Status is observability, not the
work: a database that cannot take the row logs and is ignored, because
failing the refresh over its own bookkeeping would be the wrong trade.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import SourceStatus as SourceStatusRow

log = structlog.get_logger("app.ingest.status")


@dataclass(frozen=True)
class SourceStatus:
    source_id: int
    slug: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error_class: str | None = None
    last_error_detail: str | None = None
    consecutive_failures: int = 0
    last_item_count: int = 0
    last_inserted_count: int = 0
    next_due_at: datetime | None = None

    @property
    def healthy(self) -> bool:
        return self.consecutive_failures == 0


@dataclass(frozen=True)
class PersistedStatus:
    """The durable subset — exactly the columns ``source_status`` holds."""

    source_id: int
    last_fetched_at: datetime | None
    last_success_at: datetime | None
    last_error_class: str | None
    last_error_detail: str | None
    consecutive_failures: int


def persist_source_status(
    session: Session,
    *,
    source_id: int,
    last_fetched_at: datetime | None,
    last_success_at: datetime | None,
    last_error_class: str | None,
    last_error_detail: str | None,
    consecutive_failures: int,
) -> None:
    """Upsert one row. Flushes; the caller commits."""
    values = {
        "source_id": source_id,
        "last_fetched_at": last_fetched_at,
        "last_success_at": last_success_at,
        "last_error_class": last_error_class,
        "last_error_detail": last_error_detail,
        "consecutive_failures": consecutive_failures,
    }
    updates = {key: value for key, value in values.items() if key != "source_id"}

    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgres_insert(SourceStatusRow).values(values)
        session.execute(statement.on_conflict_do_update(index_elements=["source_id"], set_=updates))
    elif dialect == "sqlite":
        sqlite_statement = sqlite_insert(SourceStatusRow).values(values)
        session.execute(
            sqlite_statement.on_conflict_do_update(index_elements=["source_id"], set_=updates)
        )
    else:  # pragma: no cover - the PRD scopes the service to these two engines
        raise RuntimeError(f"persist_source_status has no implementation for dialect {dialect!r}")
    session.flush()


def load_source_status(session: Session, source_id: int) -> PersistedStatus | None:
    row = session.get(SourceStatusRow, source_id)
    if row is None:
        return None
    return PersistedStatus(
        source_id=row.source_id,
        last_fetched_at=_as_utc(row.last_fetched_at),
        last_success_at=_as_utc(row.last_success_at),
        last_error_class=row.last_error_class,
        last_error_detail=row.last_error_detail,
        consecutive_failures=row.consecutive_failures,
    )


class SourceStatusRegistry:
    """Thread-safe: the scheduler's worker pool writes concurrently.

    Given a ``session_factory`` it writes through to ``source_status`` and
    reads back from it when this process has no memory of a source.
    Without one it is purely in-process, which is what the unit tests use.
    """

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._lock = threading.Lock()
        self._statuses: dict[int, SourceStatus] = {}
        self._session_factory = session_factory

    # -- reads -----------------------------------------------------------

    def get(self, source_id: int) -> SourceStatus | None:
        with self._lock:
            return self._statuses.get(source_id)

    def snapshot(self) -> list[SourceStatus]:
        with self._lock:
            return sorted(self._statuses.values(), key=lambda status: status.slug)

    def failing(self) -> list[SourceStatus]:
        return [status for status in self.snapshot() if not status.healthy]

    def is_due(
        self,
        source_id: int,
        *,
        refresh_minutes: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        """A source never attempted is due immediately.

        Memory answers first — it is the same process that did the work.
        A source this process has not seen falls back to the persisted
        ``last_fetched_at``, which is what stops a restart re-fetching
        the whole catalogue. ``refresh_minutes`` is needed to turn that
        timestamp into a due time; without it the answer is "due", which
        is the pre-persistence behaviour.
        """
        moment = now or datetime.now(UTC)
        with self._lock:
            status = self._statuses.get(source_id)
        if status is not None:
            return status.next_due_at is None or status.next_due_at <= moment

        persisted = self._load(source_id)
        if persisted is None or persisted.last_fetched_at is None or refresh_minutes is None:
            return True
        due_at = persisted.last_fetched_at + _interval(
            refresh_minutes, persisted.consecutive_failures
        )
        return due_at <= moment

    # -- writes ----------------------------------------------------------

    def record_success(
        self,
        *,
        source_id: int,
        slug: str,
        refresh_minutes: int,
        item_count: int,
        inserted_count: int,
        now: datetime | None = None,
    ) -> SourceStatus:
        moment = now or datetime.now(UTC)
        return self._update(
            source_id,
            slug,
            last_attempt_at=moment,
            last_success_at=moment,
            last_error_class=None,
            last_error_detail=None,
            consecutive_failures=0,
            last_item_count=item_count,
            last_inserted_count=inserted_count,
            next_due_at=moment + timedelta(minutes=refresh_minutes),
        )

    def record_failure(
        self,
        *,
        source_id: int,
        slug: str,
        refresh_minutes: int,
        error_class: str,
        detail: str,
        now: datetime | None = None,
    ) -> SourceStatus:
        moment = now or datetime.now(UTC)
        with self._lock:
            previous = self._statuses.get(source_id)
        if previous is not None:
            failures = previous.consecutive_failures + 1
            last_success_at = previous.last_success_at
        else:
            # Cold process. Without this the first failure after a restart
            # would reset the back-off to one and erase the last known
            # success from the operator's view.
            persisted = self._load(source_id)
            failures = (persisted.consecutive_failures if persisted else 0) + 1
            last_success_at = persisted.last_success_at if persisted else None
        return self._update(
            source_id,
            slug,
            last_attempt_at=moment,
            last_success_at=last_success_at,
            last_failure_at=moment,
            last_error_class=error_class,
            last_error_detail=detail[:500],
            consecutive_failures=failures,
            next_due_at=moment + _backoff(refresh_minutes, failures),
        )

    def forget(self, source_id: int) -> None:
        """Drop a source that is no longer configured or enabled."""
        with self._lock:
            self._statuses.pop(source_id, None)

    def _update(self, source_id: int, slug: str, **fields: object) -> SourceStatus:
        with self._lock:
            current = self._statuses.get(source_id) or SourceStatus(source_id=source_id, slug=slug)
            updated = replace(current, slug=slug, **fields)  # type: ignore[arg-type]
            self._statuses[source_id] = updated
        self._persist(updated)
        return updated

    # -- persistence -----------------------------------------------------

    def _persist(self, status: SourceStatus) -> None:
        if self._session_factory is None:
            return
        try:
            with self._session_factory() as session:
                persist_source_status(
                    session,
                    source_id=status.source_id,
                    last_fetched_at=status.last_attempt_at,
                    last_success_at=status.last_success_at,
                    last_error_class=status.last_error_class,
                    last_error_detail=status.last_error_detail,
                    consecutive_failures=status.consecutive_failures,
                )
                session.commit()
        except SQLAlchemyError as exc:
            log.warning(
                "source_status_persist_failed",
                source_id=status.source_id,
                source_slug=status.slug,
                error_class=type(exc).__name__,
            )

    def _load(self, source_id: int) -> PersistedStatus | None:
        if self._session_factory is None:
            return None
        try:
            with self._session_factory() as session:
                return load_source_status(session, source_id)
        except SQLAlchemyError as exc:
            log.warning(
                "source_status_load_failed",
                source_id=source_id,
                error_class=type(exc).__name__,
            )
            return None


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes for ``DateTime(timezone=True)``."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _interval(refresh_minutes: int, consecutive_failures: int) -> timedelta:
    if consecutive_failures == 0:
        return timedelta(minutes=refresh_minutes)
    return _backoff(refresh_minutes, consecutive_failures)


def _backoff(refresh_minutes: int, consecutive_failures: int) -> timedelta:
    """Exponential back-off on repeated failure, capped at six hours.

    A source that is down should not be hammered at its ordinary refresh
    interval, and a wedged source should not dominate the tick.
    """
    factor = min(2 ** (consecutive_failures - 1), 16)
    return timedelta(minutes=min(refresh_minutes * factor, 360))
