"""In-process APScheduler: refresh due sources, prune old items.

One periodic tick rather than one APScheduler job per source. Sources
are database rows an operator changes at runtime, and a job-per-source
design would need the job store re-synchronised on every change; a tick
that asks "what is due?" needs nothing re-synchronised and handles a
source appearing, disappearing, or changing its interval for free.

Concurrency between replicas is handled by the lock, not the schedule:
each source is refreshed under ``sre-tab:source:<id>`` and skipped
without waiting if another replica holds it. See ``locks.py`` for the
SQLite degradation.

``settings.source_refresh_enabled = False`` is the test and maintenance
posture: nothing is scheduled, no thread starts, and the readiness probe
reports ready-and-disabled rather than failing.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.health import ProbeResult, probes
from app.ingest.service import IngestService
from app.scheduler.locks import PRUNE_LOCK_NAME, LeaderLock, build_leader_lock, source_lock_name
from app.settings import Settings

log = structlog.get_logger("app.scheduler")

#: How often the tick asks the database what is due. Sources refresh on
#: whole-minute intervals, so a finer tick buys nothing.
TICK_SECONDS = 60
PRUNE_INTERVAL_SECONDS = 24 * 60 * 60

#: A tick older than this means the scheduler thread has stopped doing
#: work even though APScheduler still says it is running.
STALE_TICK_FACTOR = 5

PROBE_NAME = "scheduler"


class SchedulerService:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        session_factory: sessionmaker[Session],
        *,
        ingest: IngestService | None = None,
        lock: LeaderLock | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self.ingest = ingest or IngestService(session_factory, settings)
        self._lock = lock if lock is not None else build_leader_lock(engine)
        self._scheduler: BackgroundScheduler | None = None
        self._state_lock = threading.Lock()
        self._last_tick_at: datetime | None = None
        self._last_prune_at: datetime | None = None
        self._started = False

    # -- lifecycle -------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._settings.source_refresh_enabled

    @property
    def running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    def start(self) -> None:
        """Start the scheduler, or do nothing if refresh is disabled."""
        if not self.enabled:
            log.info("scheduler_disabled", detail="source_refresh_enabled is false")
            self._started = True
            return
        if self.running:
            return

        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(
            self.tick,
            "interval",
            seconds=TICK_SECONDS,
            id="refresh_due_sources",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=TICK_SECONDS,
            next_run_time=datetime.now(UTC),
        )
        scheduler.add_job(
            self.prune,
            "interval",
            seconds=PRUNE_INTERVAL_SECONDS,
            id="prune_feed_items",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=PRUNE_INTERVAL_SECONDS,
        )
        scheduler.start()
        self._scheduler = scheduler
        self._started = True
        log.info(
            "scheduler_started",
            tick_seconds=TICK_SECONDS,
            lock_kind=self._lock.kind,
            retention_days=self._settings.feed_retention_days,
        )

    def shutdown(self, *, wait: bool = False) -> None:
        scheduler, self._scheduler = self._scheduler, None
        self._started = False
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=wait)
            log.info("scheduler_stopped")

    # -- jobs ------------------------------------------------------------

    def tick(self, *, now: datetime | None = None) -> int:
        """Refresh every due source we can take the lock for.

        Returns the number of sources actually refreshed. Never raises:
        an exception escaping here would kill the APScheduler job.
        """
        moment = now or datetime.now(UTC)
        refreshed = 0
        try:
            for source in self.ingest.due_sources(now=moment):
                with self._lock.acquire(source_lock_name(source.id)) as acquired:
                    if not acquired:
                        log.debug(
                            "source_refresh_skipped",
                            source_id=source.id,
                            source_slug=source.slug,
                            detail="another replica holds the lock",
                        )
                        continue
                    self.ingest.refresh_source(source, now=moment)
                    refreshed += 1
        except Exception as exc:
            # Deliberately broad: the tick must survive anything.
            log.exception("scheduler_tick_failed", error_class=type(exc).__name__)
        finally:
            with self._state_lock:
                self._last_tick_at = datetime.now(UTC)
        return refreshed

    def prune(self, *, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        removed = 0
        try:
            with self._lock.acquire(PRUNE_LOCK_NAME) as acquired:
                if acquired:
                    removed = self.ingest.prune(now=moment)
        except Exception as exc:
            # Deliberately broad: the prune job must survive anything.
            log.exception("scheduler_prune_failed", error_class=type(exc).__name__)
        finally:
            with self._state_lock:
                self._last_prune_at = datetime.now(UTC)
        return removed

    # -- readiness -------------------------------------------------------

    def readiness(self, *, now: datetime | None = None) -> ProbeResult:
        """Ready means "feeds will refresh", not merely "process alive"."""
        if not self.enabled:
            return ProbeResult(ok=True, detail="disabled: source_refresh_enabled is false")
        if not self._started:
            return ProbeResult(ok=False, detail="not started")
        if not self.running:
            return ProbeResult(ok=False, detail="scheduler is not running")

        with self._state_lock:
            last_tick = self._last_tick_at
        if last_tick is None:
            return ProbeResult(ok=True, detail=f"running ({self._lock.kind}), awaiting first tick")

        moment = now or datetime.now(UTC)
        age = moment - last_tick
        if age > timedelta(seconds=TICK_SECONDS * STALE_TICK_FACTOR):
            return ProbeResult(ok=False, detail=f"last tick {int(age.total_seconds())}s ago")

        failing = self.ingest.status.failing()
        detail = f"running ({self._lock.kind}), last tick {int(age.total_seconds())}s ago"
        if failing:
            # Degraded sources are reported, not fatal: one broken feed
            # must not take the instance out of the load balancer.
            detail += f", {len(failing)} source(s) failing"
        return ProbeResult(ok=True, detail=detail)

    def register_probe(self) -> None:
        probes.register_readiness(PROBE_NAME, self.readiness)
