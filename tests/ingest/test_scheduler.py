"""Leader locks, the tick, the prune job, and the readiness probe."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import FeedItem, Source
from app.health import probes
from app.ingest.service import IngestService
from app.main import create_app
from app.scheduler import install_scheduler
from app.scheduler.locks import (
    PRUNE_LOCK_NAME,
    LeaderLock,
    PostgresAdvisoryLock,
    SingleProcessLock,
    advisory_key,
    build_leader_lock,
    source_lock_name,
)
from app.scheduler.service import PROBE_NAME, TICK_SECONDS, SchedulerService
from app.settings import Settings
from tests.ingest.conftest import PINNED_URL

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


class NeverAcquiresLock(LeaderLock):
    """Stands in for another replica already holding every lock."""

    kind = "never"

    def __init__(self) -> None:
        self.requested: list[str] = []

    def acquire(self, name: str) -> AbstractContextManager[bool]:
        self.requested.append(name)
        return self._acquire()

    @contextmanager
    def _acquire(self) -> Iterator[bool]:
        yield False


@pytest.fixture
def scheduler(
    ingest_settings: Settings,
    engine: Engine,
    session_factory: sessionmaker[Session],
    ingest_service: IngestService,
) -> SchedulerService:
    return SchedulerService(
        ingest_settings, engine, session_factory, ingest=ingest_service, lock=SingleProcessLock()
    )


# --- lock primitives ----------------------------------------------------


def test_advisory_keys_are_stable_and_distinct() -> None:
    assert advisory_key("sre-tab:source:1") == advisory_key("sre-tab:source:1")
    assert advisory_key("sre-tab:source:1") != advisory_key("sre-tab:source:2")
    key = advisory_key(PRUNE_LOCK_NAME)
    # Must fit PostgreSQL's signed bigint.
    assert -(2**63) <= key < 2**63


def test_source_lock_names_are_per_source() -> None:
    assert source_lock_name(1) != source_lock_name(2)


def test_single_process_lock_excludes_within_the_process() -> None:
    lock = SingleProcessLock()
    with lock.acquire("a") as first:
        assert first is True
        with lock.acquire("a") as second:
            assert second is False
    # Released on exit.
    with lock.acquire("a") as third:
        assert third is True


def test_single_process_lock_does_not_couple_names() -> None:
    lock = SingleProcessLock()
    with lock.acquire("a") as first, lock.acquire("b") as second:
        assert first is True
        assert second is True


def test_single_process_lock_releases_after_an_exception() -> None:
    lock = SingleProcessLock()
    with pytest.raises(RuntimeError), lock.acquire("a") as acquired:
        assert acquired is True
        raise RuntimeError("boom")
    with lock.acquire("a") as again:
        assert again is True


def test_single_process_lock_is_thread_safe() -> None:
    lock = SingleProcessLock()
    winners: list[bool] = []
    started = threading.Barrier(4)

    def contend() -> None:
        started.wait()
        with lock.acquire("shared") as acquired:
            winners.append(acquired)
            if acquired:
                threading.Event().wait(0.02)

    threads = [threading.Thread(target=contend) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(winners) == 1


def test_sqlite_degrades_to_the_single_process_lock(engine: Engine) -> None:
    lock = build_leader_lock(engine)
    assert isinstance(lock, SingleProcessLock)
    assert lock.kind == "single-process"


def test_postgres_selects_the_advisory_lock() -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeEngine:
        dialect = FakeDialect()

    lock = build_leader_lock(FakeEngine())  # type: ignore[arg-type]
    assert isinstance(lock, PostgresAdvisoryLock)
    assert lock.kind == "postgres-advisory"


# --- the tick -----------------------------------------------------------


@respx.mock
def test_tick_refreshes_due_sources(
    scheduler: SchedulerService, source: Source, db_session: Session, rss_feed: bytes
) -> None:
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=rss_feed))
    assert scheduler.tick(now=NOW) == 1
    assert db_session.scalars(select(FeedItem.canonical_url)).all() != []


@respx.mock
def test_tick_skips_sources_not_yet_due(
    scheduler: SchedulerService, source: Source, rss_feed: bytes
) -> None:
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=rss_feed))
    assert scheduler.tick(now=NOW) == 1
    assert scheduler.tick(now=NOW + timedelta(minutes=5)) == 0
    assert scheduler.tick(now=NOW + timedelta(minutes=31)) == 1


@respx.mock
def test_tick_skips_a_source_another_replica_holds(
    ingest_settings: Settings,
    engine: Engine,
    session_factory: sessionmaker[Session],
    ingest_service: IngestService,
    source: Source,
) -> None:
    lock = NeverAcquiresLock()
    scheduler = SchedulerService(
        ingest_settings, engine, session_factory, ingest=ingest_service, lock=lock
    )
    trap = respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=b"<rss/>"))

    assert scheduler.tick(now=NOW) == 0
    assert lock.requested == [source_lock_name(source.id)]
    assert trap.call_count == 0


@respx.mock
def test_tick_survives_a_source_failure(scheduler: SchedulerService, source: Source) -> None:
    respx.get(PINNED_URL).mock(side_effect=httpx.ConnectError("down"))
    assert scheduler.tick(now=NOW) == 1
    status = scheduler.ingest.status.get(source.id)
    assert status is not None
    assert status.consecutive_failures == 1


def test_tick_survives_a_database_failure(scheduler: SchedulerService) -> None:
    def explode(**_: object) -> list[object]:
        raise OperationalError("SELECT 1", {}, Exception("connection lost"))

    scheduler.ingest.due_sources = explode  # type: ignore[method-assign, assignment]
    # An exception escaping the tick would kill the APScheduler job.
    assert scheduler.tick(now=NOW) == 0
    assert scheduler.readiness(now=NOW) is not None


# --- prune job ----------------------------------------------------------


def test_prune_job_takes_the_global_lock(
    ingest_settings: Settings,
    engine: Engine,
    session_factory: sessionmaker[Session],
    ingest_service: IngestService,
) -> None:
    lock = NeverAcquiresLock()
    scheduler = SchedulerService(
        ingest_settings, engine, session_factory, ingest=ingest_service, lock=lock
    )
    assert scheduler.prune(now=NOW) == 0
    assert lock.requested == [PRUNE_LOCK_NAME]


def test_prune_job_removes_expired_items(
    scheduler: SchedulerService, source: Source, db_session: Session
) -> None:
    from app.ingest.normalise import NormalisedItem
    from app.ingest.store import upsert_items

    upsert_items(
        db_session,
        source_id=source.id,
        items=[
            NormalisedItem(
                canonical_url="https://example.org/old",
                title="Old",
                summary=None,
                published_at=NOW - timedelta(days=400),
                image_url=None,
            )
        ],
        topic_ids=[],
        fetched_at=NOW,
    )
    assert scheduler.prune(now=NOW) == 1


# --- disabled posture ---------------------------------------------------


def test_disabled_scheduler_starts_nothing(scheduler: SchedulerService) -> None:
    assert scheduler.enabled is False
    scheduler.start()
    assert scheduler.running is False
    scheduler.shutdown()


def test_disabled_scheduler_is_ready(scheduler: SchedulerService) -> None:
    scheduler.start()
    result = scheduler.readiness()
    assert result.ok is True
    assert result.detail is not None
    assert "disabled" in result.detail


# --- readiness ----------------------------------------------------------


def test_unstarted_scheduler_is_not_ready(
    ingest_settings: Settings,
    engine: Engine,
    session_factory: sessionmaker[Session],
    ingest_service: IngestService,
) -> None:
    enabled = ingest_settings.model_copy(update={"source_refresh_enabled": True})
    scheduler = SchedulerService(
        enabled, engine, session_factory, ingest=ingest_service, lock=SingleProcessLock()
    )
    assert scheduler.readiness().ok is False


def test_enabled_scheduler_reports_running_then_stops(
    ingest_settings: Settings,
    engine: Engine,
    session_factory: sessionmaker[Session],
    ingest_service: IngestService,
) -> None:
    enabled = ingest_settings.model_copy(update={"source_refresh_enabled": True})
    scheduler = SchedulerService(
        enabled, engine, session_factory, ingest=ingest_service, lock=SingleProcessLock()
    )
    try:
        scheduler.start()
        # Assigned first: asserting on the property directly narrows it
        # for the rest of the function.
        started = scheduler.running
        assert started is True
        result = scheduler.readiness()
        assert result.ok is True
        assert result.detail is not None
        assert "single-process" in result.detail
    finally:
        scheduler.shutdown(wait=True)

    stopped = scheduler.running
    assert stopped is False
    assert scheduler.readiness().ok is False


def test_a_stale_tick_fails_readiness(
    ingest_settings: Settings,
    engine: Engine,
    session_factory: sessionmaker[Session],
    ingest_service: IngestService,
) -> None:
    enabled = ingest_settings.model_copy(update={"source_refresh_enabled": True})
    scheduler = SchedulerService(
        enabled, engine, session_factory, ingest=ingest_service, lock=SingleProcessLock()
    )
    try:
        scheduler.start()
        scheduler.tick(now=NOW)
        stale = datetime.now(UTC) + timedelta(seconds=TICK_SECONDS * 10)
        assert scheduler.readiness(now=stale).ok is False
    finally:
        scheduler.shutdown(wait=True)


def test_failing_sources_are_reported_without_failing_readiness(
    ingest_settings: Settings,
    engine: Engine,
    session_factory: sessionmaker[Session],
    ingest_service: IngestService,
) -> None:
    """One broken feed must not take the instance out of the pool.

    No source row and no respx route: ``start()`` schedules an immediate
    first tick on its own thread, and racing that thread for the same
    source is how this test used to flake. The failure is recorded
    directly instead, which is what readiness actually reads.
    """
    enabled = ingest_settings.model_copy(update={"source_refresh_enabled": True})
    scheduler = SchedulerService(
        enabled, engine, session_factory, ingest=ingest_service, lock=SingleProcessLock()
    )
    ingest_service.status.record_failure(
        source_id=1,
        slug="broken",
        refresh_minutes=30,
        error_class="FetchError",
        detail="down",
    )
    try:
        scheduler.start()
        scheduler.tick()
        result = scheduler.readiness()
        assert result.ok is True
        assert result.detail is not None
        assert "1 source(s) failing" in result.detail
    finally:
        scheduler.shutdown(wait=True)


# --- installation into the app ------------------------------------------


def test_install_scheduler_registers_the_probe_and_lifespan(
    ingest_settings: Settings, engine: Engine
) -> None:
    """The one line Phase 2 adds to ``create_app``."""
    application = create_app(ingest_settings, engine=engine)
    service = install_scheduler(application)
    assert application.state.scheduler is service
    assert PROBE_NAME in probes.run().readiness

    with TestClient(application) as client:
        body = client.get("/api/v1/healthz").json()

    # These settings disable refresh, so the instance is ready and says why.
    assert body["ready"] is True
    assert body["readiness"][PROBE_NAME]["ok"] is True
    assert "disabled" in body["readiness"][PROBE_NAME]["detail"]


def test_install_scheduler_starts_and_stops_with_the_app(
    ingest_settings: Settings, engine: Engine
) -> None:
    enabled = ingest_settings.model_copy(update={"source_refresh_enabled": True})
    application = create_app(enabled, engine=engine)
    service = install_scheduler(application)

    with TestClient(application) as client:
        assert service.running is True
        body = client.get("/api/v1/healthz").json()
        assert body["readiness"][PROBE_NAME]["ok"] is True
    assert service.running is False
