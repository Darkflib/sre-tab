"""The status registry writes through to ``source_status``.

Two things the table has to buy, and one it must not cost:

- a separate process (the operator CLI) can read refresh outcomes;
- a restarted process does not treat every source as due at once;
- a database that will not take the row does not break the refresh.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Source
from app.db.models import SourceStatus as SourceStatusRow
from app.db.session import build_session_factory
from app.ingest.status import SourceStatusRegistry

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def _row(session: Session, source_id: int) -> SourceStatusRow | None:
    return session.scalar(select(SourceStatusRow).where(SourceStatusRow.source_id == source_id))


# --- write-through ------------------------------------------------------


def test_success_is_written_where_another_process_can_read_it(
    session_factory: sessionmaker[Session], db_session: Session, source: Source
) -> None:
    registry = SourceStatusRegistry(session_factory)
    registry.record_success(
        source_id=source.id,
        slug=source.slug,
        refresh_minutes=30,
        item_count=5,
        inserted_count=2,
        now=NOW,
    )

    # A different session, standing in for the CLI process.
    row = _row(db_session, source.id)
    assert row is not None
    assert row.consecutive_failures == 0
    assert row.last_error_class is None
    assert row.last_fetched_at is not None
    assert row.last_success_at is not None


def test_failure_is_written_with_its_classification(
    session_factory: sessionmaker[Session], db_session: Session, source: Source
) -> None:
    registry = SourceStatusRegistry(session_factory)
    registry.record_failure(
        source_id=source.id,
        slug=source.slug,
        refresh_minutes=30,
        error_class="FetchTimeoutError",
        detail="timed out after 10s",
        now=NOW,
    )

    row = _row(db_session, source.id)
    assert row is not None
    assert row.consecutive_failures == 1
    assert row.last_error_class == "FetchTimeoutError"
    assert row.last_error_detail == "timed out after 10s"
    assert row.last_success_at is None


def test_repeated_outcomes_update_one_row(
    session_factory: sessionmaker[Session], db_session: Session, source: Source
) -> None:
    registry = SourceStatusRegistry(session_factory)
    for index in range(3):
        registry.record_failure(
            source_id=source.id,
            slug=source.slug,
            refresh_minutes=30,
            error_class="FetchError",
            detail="down",
            now=NOW + timedelta(minutes=index),
        )

    rows = db_session.scalars(select(SourceStatusRow)).all()
    assert len(rows) == 1
    assert rows[0].consecutive_failures == 3


def test_a_success_clears_the_persisted_error(
    session_factory: sessionmaker[Session], db_session: Session, source: Source
) -> None:
    registry = SourceStatusRegistry(session_factory)
    registry.record_failure(
        source_id=source.id,
        slug=source.slug,
        refresh_minutes=30,
        error_class="FetchError",
        detail="down",
        now=NOW,
    )
    registry.record_success(
        source_id=source.id,
        slug=source.slug,
        refresh_minutes=30,
        item_count=1,
        inserted_count=1,
        now=NOW + timedelta(minutes=30),
    )

    db_session.expire_all()
    row = _row(db_session, source.id)
    assert row is not None
    assert row.consecutive_failures == 0
    assert row.last_error_class is None


# --- restart and replica behaviour --------------------------------------


def test_a_cold_registry_reads_the_persisted_schedule(
    session_factory: sessionmaker[Session], source: Source
) -> None:
    """The per-replica drift agent B flagged: without the persisted
    ``last_fetched_at``, a restart re-fetches the whole catalogue."""
    SourceStatusRegistry(session_factory).record_success(
        source_id=source.id,
        slug=source.slug,
        refresh_minutes=30,
        item_count=1,
        inserted_count=1,
        now=NOW,
    )

    # A second process — or the same one after a restart.
    cold = SourceStatusRegistry(session_factory)
    assert cold.get(source.id) is None
    assert cold.is_due(source.id, refresh_minutes=30, now=NOW + timedelta(minutes=5)) is False
    assert cold.is_due(source.id, refresh_minutes=30, now=NOW + timedelta(minutes=31)) is True


def test_a_cold_registry_honours_the_persisted_back_off(
    session_factory: sessionmaker[Session], source: Source
) -> None:
    warm = SourceStatusRegistry(session_factory)
    for index in range(3):
        warm.record_failure(
            source_id=source.id,
            slug=source.slug,
            refresh_minutes=30,
            error_class="FetchError",
            detail="down",
            now=NOW + timedelta(seconds=index),
        )

    # Three failures back off to 4 x 30 minutes, and the restart must not
    # reset that to the ordinary interval.
    cold = SourceStatusRegistry(session_factory)
    assert cold.is_due(source.id, refresh_minutes=30, now=NOW + timedelta(minutes=90)) is False
    assert cold.is_due(source.id, refresh_minutes=30, now=NOW + timedelta(minutes=121)) is True


def test_a_cold_registry_continues_the_failure_count(
    session_factory: sessionmaker[Session], db_session: Session, source: Source
) -> None:
    """A restart must not reset the back-off, nor erase the last success."""
    warm = SourceStatusRegistry(session_factory)
    warm.record_success(
        source_id=source.id,
        slug=source.slug,
        refresh_minutes=30,
        item_count=1,
        inserted_count=1,
        now=NOW,
    )
    warm.record_failure(
        source_id=source.id,
        slug=source.slug,
        refresh_minutes=30,
        error_class="FetchError",
        detail="down",
        now=NOW + timedelta(minutes=30),
    )

    cold = SourceStatusRegistry(session_factory)
    status = cold.record_failure(
        source_id=source.id,
        slug=source.slug,
        refresh_minutes=30,
        error_class="FetchError",
        detail="still down",
        now=NOW + timedelta(minutes=90),
    )
    assert status.consecutive_failures == 2
    assert status.last_success_at is not None

    db_session.expire_all()
    row = _row(db_session, source.id)
    assert row is not None
    assert row.consecutive_failures == 2
    assert row.last_success_at is not None


def test_a_never_fetched_source_is_due_even_with_a_table(
    session_factory: sessionmaker[Session], source: Source
) -> None:
    assert SourceStatusRegistry(session_factory).is_due(source.id, refresh_minutes=30) is True


# --- degradation --------------------------------------------------------


def test_persistence_failure_does_not_break_the_refresh(source: Source, engine: Engine) -> None:
    """Status is observability. Losing it must not lose the fetch.

    An un-migrated database is the realistic version of this: the table
    is simply not there, and the refresh has to carry on regardless.
    """
    SourceStatusRow.metadata.tables["source_status"].drop(engine)

    registry = SourceStatusRegistry(build_session_factory(engine))
    status = registry.record_failure(
        source_id=source.id,
        slug=source.slug,
        refresh_minutes=30,
        error_class="FetchError",
        detail="down",
        now=NOW,
    )
    assert status.consecutive_failures == 1


def test_a_registry_without_a_factory_stays_in_process(source: Source) -> None:
    registry = SourceStatusRegistry()
    registry.record_success(
        source_id=source.id,
        slug=source.slug,
        refresh_minutes=30,
        item_count=1,
        inserted_count=1,
        now=NOW,
    )
    assert registry.is_due(source.id, refresh_minutes=30, now=NOW + timedelta(minutes=5)) is False
