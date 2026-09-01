"""The session sweep: what it deletes, what it must not, and what it owns.

Nothing else in the system deletes from ``sessions``, so this function is
the only thing standing between a long-lived instance and a table with one
row per sign-in ever made. That makes the *negative* assertions the
important ones. A sweep that removes too little costs disk; a sweep that
removes a live row signs everyone out, and does it silently, on a timer,
at four in the morning.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.sessions import REVOKED_RETENTION_DAYS, prune_sessions, resolve_session
from app.db.models import User, UserSession
from app.security.tokens import generate_session_token, hash_session_token

#: Anchored to the real clock rather than a literal, and not for want of
#: determinism — every ``prune_sessions`` call below passes ``now=NOW``
#: explicitly, so the sweep itself is exact. The assertions that a survivor
#: is still *usable* go through ``resolve_session``, which reads
#: ``datetime.now(UTC)`` and cannot be told otherwise. Against a frozen
#: literal those rows are "live" to the sweep and long expired to the
#: resolver the moment the literal falls into the past — which it did, and
#: which is how this comment came to be here.
NOW = datetime.now(UTC)


def _store_session(db: Session, user: User, **overrides: object) -> str:
    """A session row, committed, so a later rollback cannot take it back.

    Committing matters for ``test_prune_does_not_commit``: the rows have to
    predate the transaction under test for the rollback there to prove
    anything.
    """
    token = generate_session_token()
    values: dict[str, object] = {
        "user_id": user.id,
        "token_hash": hash_session_token(token),
        "expires_at": NOW + timedelta(days=1),
    }
    values.update(overrides)
    db.add(UserSession(**values))
    db.commit()
    return token


def _count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(UserSession)) or 0


# --- what survives ------------------------------------------------------


def test_a_live_session_survives(db_session: Session, test_user: User) -> None:
    """The one that matters. A live session is unrevoked and unexpired,
    and deleting it is an unannounced logout for a signed-in user."""
    token = _store_session(db_session, test_user)

    assert prune_sessions(db_session, now=NOW) == 0
    assert _count(db_session) == 1

    # Not merely present — still usable. A row that survived the delete but
    # lost its ability to authenticate would pass a row count and fail the
    # user.
    resolved = resolve_session(db_session, token)
    assert resolved is not None
    assert resolved.id == test_user.id


def test_a_session_expiring_later_today_survives(db_session: Session, test_user: User) -> None:
    """The boundary from the live side: still valid, if only just."""
    _store_session(db_session, test_user, expires_at=NOW + timedelta(seconds=1))
    assert prune_sessions(db_session, now=NOW) == 0
    assert _count(db_session) == 1


def test_a_recently_revoked_session_survives_the_grace_window(
    db_session: Session, test_user: User
) -> None:
    """The forensic record of a logout outlives the logout by a week."""
    _store_session(db_session, test_user, revoked_at=NOW - timedelta(days=1))
    assert prune_sessions(db_session, now=NOW) == 0
    assert _count(db_session) == 1


def test_a_revoked_and_expired_session_is_held_by_the_grace_window(
    db_session: Session, test_user: User
) -> None:
    """The interaction the two branches have to get right.

    Revoked an hour ago, expired half an hour ago. The expiry branch must
    not claim it: the value is in the revocation timestamp, and that does
    not stop mattering because the session would have timed out anyway.
    """
    _store_session(
        db_session,
        test_user,
        revoked_at=NOW - timedelta(hours=1),
        expires_at=NOW - timedelta(minutes=30),
    )
    assert prune_sessions(db_session, now=NOW) == 0
    assert _count(db_session) == 1


# --- what goes ----------------------------------------------------------


def test_an_expired_session_is_deleted(db_session: Session, test_user: User) -> None:
    _store_session(db_session, test_user, expires_at=NOW - timedelta(minutes=1))
    assert prune_sessions(db_session, now=NOW) == 1
    assert _count(db_session) == 0


def test_a_session_expiring_exactly_now_is_deleted(db_session: Session, test_user: User) -> None:
    """The boundary from the dead side. ``expires_at <= now`` matches the
    ``expires_at > now`` that ``resolve_session`` requires for liveness, so
    the two predicates meet exactly and leave no row that is neither."""
    _store_session(db_session, test_user, expires_at=NOW)
    assert prune_sessions(db_session, now=NOW) == 1
    assert _count(db_session) == 0


def test_a_session_revoked_beyond_the_grace_window_is_deleted(
    db_session: Session, test_user: User
) -> None:
    _store_session(
        db_session,
        test_user,
        revoked_at=NOW - timedelta(days=REVOKED_RETENTION_DAYS, minutes=1),
        # Deliberately still unexpired: the revocation branch has to stand
        # on its own, not be carried by the expiry one.
        expires_at=NOW + timedelta(days=365),
    )
    assert prune_sessions(db_session, now=NOW) == 1
    assert _count(db_session) == 0


def test_the_grace_window_is_configurable(db_session: Session, test_user: User) -> None:
    _store_session(db_session, test_user, revoked_at=NOW - timedelta(days=2))

    assert prune_sessions(db_session, now=NOW, revoked_retention_days=3) == 0
    assert prune_sessions(db_session, now=NOW, revoked_retention_days=1) == 1


def test_a_zero_grace_window_deletes_revoked_sessions_at_once(
    db_session: Session, test_user: User
) -> None:
    """The operator's escape hatch, for an instance that would rather keep
    nothing. Offered, not the default."""
    _store_session(db_session, test_user, revoked_at=NOW - timedelta(seconds=1))
    assert prune_sessions(db_session, now=NOW, revoked_retention_days=0) == 1


# --- counting and mixed populations -------------------------------------


def test_the_count_is_the_number_of_rows_actually_deleted(
    db_session: Session, test_user: User
) -> None:
    """Three dead by two different routes, two live. The returned number is
    what the CLI prints and what an operator reasons about, so it has to be
    the truth rather than the number of candidates considered."""
    live = _store_session(db_session, test_user)
    _store_session(db_session, test_user, revoked_at=NOW - timedelta(hours=2))  # in grace
    _store_session(db_session, test_user, expires_at=NOW - timedelta(days=1))
    _store_session(db_session, test_user, expires_at=NOW - timedelta(days=30))
    _store_session(db_session, test_user, revoked_at=NOW - timedelta(days=90))

    assert _count(db_session) == 5
    assert prune_sessions(db_session, now=NOW) == 3
    assert _count(db_session) == 2
    assert resolve_session(db_session, live) is not None


def test_an_empty_table_prunes_to_zero(db_session: Session) -> None:
    assert prune_sessions(db_session, now=NOW) == 0


def test_one_users_dead_sessions_do_not_take_anothers_live_one(db_session: Session) -> None:
    """``sessions.user_id`` is not in the predicate, and this is the test
    that says so on purpose rather than by omission."""
    signed_in = User(github_id=1000001, github_login="octocat", display_name="Octo Cat")
    departed = User(github_id=2000002, github_login="monalisa", display_name="Mona Lisa")
    db_session.add_all([signed_in, departed])
    db_session.commit()

    live = _store_session(db_session, signed_in)
    _store_session(db_session, departed, expires_at=NOW - timedelta(days=1))

    assert prune_sessions(db_session, now=NOW) == 1
    assert resolve_session(db_session, live) is not None


def test_pruning_twice_is_idempotent(db_session: Session, test_user: User) -> None:
    _store_session(db_session, test_user, expires_at=NOW - timedelta(days=1))
    assert prune_sessions(db_session, now=NOW) == 1
    assert prune_sessions(db_session, now=NOW) == 0


def test_a_default_now_sweeps_against_the_wall_clock(db_session: Session, test_user: User) -> None:
    """``now`` is injected for determinism, but the production caller omits
    it — so the default has to work rather than merely exist."""
    _store_session(db_session, test_user, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    _store_session(db_session, test_user, expires_at=datetime.now(UTC) + timedelta(days=1))
    assert prune_sessions(db_session) == 1
    assert _count(db_session) == 1


# --- the transaction rule -----------------------------------------------


def test_prune_does_not_commit(db_session: Session, test_user: User) -> None:
    """AGENTS.md, "Transactions": a function that *receives* a Session never
    commits. If it did, the OAuth callback's one-transaction guarantee would
    not survive this being called anywhere near it.

    Rolling back after the sweep is the direct proof: an uncommitted delete
    comes back, a committed one does not.
    """
    _store_session(db_session, test_user, expires_at=NOW - timedelta(days=1))

    assert prune_sessions(db_session, now=NOW) == 1
    assert _count(db_session) == 0

    db_session.rollback()
    assert _count(db_session) == 1


def test_prune_does_not_call_commit_or_rollback(
    db_session: Session, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule from the other side, so a future implementation that
    committed *and* re-inserted could not slip past the rollback test."""
    _store_session(db_session, test_user, expires_at=NOW - timedelta(days=1))

    called: list[str] = []
    monkeypatch.setattr(db_session, "commit", lambda: called.append("commit"))
    monkeypatch.setattr(db_session, "rollback", lambda: called.append("rollback"))

    prune_sessions(db_session, now=NOW)
    assert called == []
