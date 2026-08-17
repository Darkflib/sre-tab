"""The advisory-lock leader strategy, against a live server.

Unit tests pick the strategy by dialect name and stop there — they prove
``build_leader_lock`` returns the right class, not that
``pg_try_advisory_lock`` behaves. Two replicas racing for one source is
the failure this guards against, and it only exists on PostgreSQL.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import Engine, text

from app.scheduler.locks import (
    PRUNE_LOCK_NAME,
    PostgresAdvisoryLock,
    advisory_key,
    build_leader_lock,
    source_lock_name,
)
from tests.postgres.conftest import pytestmark as _pytestmark

pytestmark = _pytestmark


def _held(engine: Engine, name: str) -> bool:
    """Is this advisory lock held by any session on the server?

    ``pg_locks`` splits the 64-bit key across ``classid`` (high half) and
    ``objid`` (low half), with ``objsubid = 1`` for the single-bigint
    form the scheduler uses.
    """
    unsigned = advisory_key(name) & 0xFFFF_FFFF_FFFF_FFFF
    with engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND objsubid = 1 "
                "AND classid = :classid AND objid = :objid AND granted"
            ),
            {"classid": unsigned >> 32, "objid": unsigned & 0xFFFF_FFFF},
        ).scalar_one()
    return bool(count)


def test_a_real_engine_selects_the_advisory_lock(pg_engine: Engine) -> None:
    lock = build_leader_lock(pg_engine)
    assert isinstance(lock, PostgresAdvisoryLock)
    assert lock.kind == "postgres-advisory"


def test_a_second_holder_is_refused_and_the_first_is_released(pg_engine: Engine) -> None:
    """The whole point: two replicas, one source, one fetch."""
    replica_one = PostgresAdvisoryLock(pg_engine)
    replica_two = PostgresAdvisoryLock(pg_engine)
    name = source_lock_name(42)

    with replica_one.acquire(name) as first:
        assert first is True
        with replica_two.acquire(name) as second:
            assert second is False

    # Released on exit, so the next tick can take it.
    with replica_two.acquire(name) as third:
        assert third is True


def test_distinct_sources_do_not_contend(pg_engine: Engine) -> None:
    lock = PostgresAdvisoryLock(pg_engine)
    with lock.acquire(source_lock_name(1)) as one, lock.acquire(source_lock_name(2)) as two:
        assert one is True
        assert two is True


def test_the_lock_is_released_after_an_exception(pg_engine: Engine) -> None:
    """Session-level locks outlive a rollback, so the finally matters: a
    job that raises must not wedge its source until the process restarts."""
    lock = PostgresAdvisoryLock(pg_engine)
    with pytest.raises(RuntimeError), lock.acquire(PRUNE_LOCK_NAME) as acquired:
        assert acquired is True
        raise RuntimeError("job exploded")

    with lock.acquire(PRUNE_LOCK_NAME) as again:
        assert again is True


def test_the_lock_is_not_left_on_the_pooled_connection(pg_engine: Engine) -> None:
    """A pooled connection carries its advisory locks back to the pool.

    Leaking one would look like a source that silently stops refreshing,
    so assert the server holds nothing once the block has exited.
    """
    lock = PostgresAdvisoryLock(pg_engine)
    name = source_lock_name(7)
    with lock.acquire(name) as acquired:
        assert acquired is True
        assert _held(pg_engine, name) is True
    assert _held(pg_engine, name) is False


def test_concurrent_threads_produce_exactly_one_winner(pg_engine: Engine) -> None:
    lock = PostgresAdvisoryLock(pg_engine)
    name = source_lock_name(99)
    winners: list[bool] = []
    guard = threading.Lock()
    ready = threading.Barrier(4)

    def contend() -> None:
        ready.wait()
        with lock.acquire(name) as acquired:
            with guard:
                winners.append(acquired)
            if acquired:
                threading.Event().wait(0.05)

    threads = [threading.Thread(target=contend) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(winners) == 1
