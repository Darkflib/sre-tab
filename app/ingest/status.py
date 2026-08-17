"""Per-source refresh outcome.

Phase 2's operator CLI needs to answer "which sources are failing, and
why". The schema Phase 0 froze has nowhere to put that — there is no
``sources.last_fetched_at`` and no ``source_status`` table — so v1 keeps
it in process, alongside a structured log line per outcome carrying the
same fields. That is sufficient for the single-process v1 deployment and
is flagged as a schema gap for Phase 2; it is *not* sufficient for a
separate CLI process or for multiple replicas, and the log line is what
covers those until a table exists.

The registry also owns each source's next-due time, since without a
persisted last-fetch column there is nowhere else for it to live.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta


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


class SourceStatusRegistry:
    """Thread-safe: the scheduler's worker pool writes concurrently."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._statuses: dict[int, SourceStatus] = {}

    # -- reads -----------------------------------------------------------

    def get(self, source_id: int) -> SourceStatus | None:
        with self._lock:
            return self._statuses.get(source_id)

    def snapshot(self) -> list[SourceStatus]:
        with self._lock:
            return sorted(self._statuses.values(), key=lambda status: status.slug)

    def failing(self) -> list[SourceStatus]:
        return [status for status in self.snapshot() if not status.healthy]

    def is_due(self, source_id: int, *, now: datetime | None = None) -> bool:
        """A source never attempted is due immediately."""
        moment = now or datetime.now(UTC)
        with self._lock:
            status = self._statuses.get(source_id)
        return status is None or status.next_due_at is None or status.next_due_at <= moment

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
            failures = (previous.consecutive_failures if previous else 0) + 1
        return self._update(
            source_id,
            slug,
            last_attempt_at=moment,
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
            return updated


def _backoff(refresh_minutes: int, consecutive_failures: int) -> timedelta:
    """Exponential back-off on repeated failure, capped at six hours.

    A source that is down should not be hammered at its ordinary refresh
    interval, and a wedged source should not dominate the tick.
    """
    factor = min(2 ** (consecutive_failures - 1), 16)
    return timedelta(minutes=min(refresh_minutes * factor, 360))
