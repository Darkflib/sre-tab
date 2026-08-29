"""``sre-tab sessions prune`` — the front end the systemd timer calls.

Driven through ``main`` against a migrated file database rather than the
shared in-memory engine, like the rest of the CLI suite: the session and
engine handling in :mod:`app.cli` is part of what is under test, and an
in-memory database shared with the test would hide a commit that never
happened.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.cli import main
from app.db.engine import create_db_engine
from app.db.models import User, UserSession
from app.db.session import build_session_factory
from app.security.tokens import generate_session_token, hash_session_token
from tests.cli.test_operations import _migrated

NOW = datetime.now(UTC)


def _database(tmp_path_factory: pytest.TempPathFactory, name: str) -> str:
    path: pathlib.Path = tmp_path_factory.mktemp(name) / "cli.db"
    return _migrated(path)


def _seed_sessions(url: str, *, live: int = 0, expired: int = 0, revoked_days_ago: int = 0) -> None:
    """Put a user and some session rows in the database and commit.

    ``revoked_days_ago`` adds a single revoked session that far in the past;
    zero adds none.
    """
    engine: Engine = create_db_engine(url)
    try:
        with build_session_factory(engine)() as session:
            user = User(github_id=1000001, github_login="octocat", display_name="Octo Cat")
            session.add(user)
            session.flush()
            for _ in range(live):
                _add(session, user, expires_at=NOW + timedelta(days=1))
            for _ in range(expired):
                _add(session, user, expires_at=NOW - timedelta(days=1))
            if revoked_days_ago:
                _add(
                    session,
                    user,
                    expires_at=NOW + timedelta(days=1),
                    revoked_at=NOW - timedelta(days=revoked_days_ago),
                )
            session.commit()
    finally:
        engine.dispose()


def _add(session: Session, user: User, **values: object) -> None:
    session.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_session_token(generate_session_token()),
            **values,
        )
    )


def _count(url: str) -> int:
    engine: Engine = create_db_engine(url)
    try:
        with build_session_factory(engine)() as session:
            return session.scalar(select(func.count()).select_from(UserSession)) or 0
    finally:
        engine.dispose()


def test_prune_deletes_dead_rows_and_reports_the_count(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    url = _database(tmp_path_factory, "prune")
    _seed_sessions(url, live=1, expired=2)

    assert main(["--database-url", url, "sessions", "prune"]) == 0
    assert "deleted 2 dead session rows" in capsys.readouterr().out
    # The live one is still there — the CLI commits the delete, and commits
    # only the delete.
    assert _count(url) == 1


def test_prune_commits(tmp_path_factory: pytest.TempPathFactory) -> None:
    """``prune_sessions`` deliberately does not commit, so the subcommand
    must — otherwise the sweep runs nightly, reports a number, and rolls
    every one of them back when the session closes."""
    url = _database(tmp_path_factory, "commits")
    _seed_sessions(url, expired=3)

    assert main(["--database-url", url, "sessions", "prune"]) == 0
    # Read back through a wholly separate engine and connection.
    assert _count(url) == 0


def test_prune_says_so_when_there_is_nothing_to_do(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """The steady state on a quiet instance, and still a clean exit: the
    timer firing nightly against nothing is not a fault."""
    url = _database(tmp_path_factory, "quiet")
    _seed_sessions(url, live=2)

    assert main(["--database-url", url, "sessions", "prune"]) == 0
    assert "no dead sessions; nothing to do" in capsys.readouterr().out
    assert _count(url) == 2


def test_prune_on_an_empty_database_is_a_clean_exit(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    url = _database(tmp_path_factory, "empty")
    assert main(["--database-url", url, "sessions", "prune"]) == 0


def test_prune_counts_one_row_in_the_singular(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    url = _database(tmp_path_factory, "singular")
    _seed_sessions(url, expired=1)

    assert main(["--database-url", url, "sessions", "prune"]) == 0
    assert "deleted 1 dead session row\n" in capsys.readouterr().out


def test_the_default_grace_window_keeps_a_recently_revoked_session(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """The retention policy reaches the operator through the default, so the
    default is what the timer's behaviour actually depends on."""
    url = _database(tmp_path_factory, "grace")
    _seed_sessions(url, revoked_days_ago=1)

    assert main(["--database-url", url, "sessions", "prune"]) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert _count(url) == 1


def test_the_grace_window_can_be_overridden(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    url = _database(tmp_path_factory, "override")
    _seed_sessions(url, revoked_days_ago=3)

    assert main(["--database-url", url, "sessions", "prune", "--revoked-grace-days", "2"]) == 0
    assert "deleted 1 dead session row" in capsys.readouterr().out
    assert _count(url) == 0


def test_a_negative_grace_window_is_refused(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """A negative window puts the cutoff in the future and would sweep every
    revoked row, including one revoked seconds ago. argparse exits 2, the
    same code an operator error gets from ``main``."""
    url = _database(tmp_path_factory, "negative")
    _seed_sessions(url, revoked_days_ago=1)

    with pytest.raises(SystemExit) as exit_info:
        main(["--database-url", url, "sessions", "prune", "--revoked-grace-days", "-1"])
    assert exit_info.value.code == 2
    assert "cannot be negative" in capsys.readouterr().err
    assert _count(url) == 1


def test_sessions_without_a_subcommand_is_refused(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Matches ``sources`` and ``topics``: the group is not a command."""
    with pytest.raises(SystemExit) as exit_info:
        main(["sessions"])
    assert exit_info.value.code == 2
