"""Two concurrent first sign-ins for one GitHub account.

The window ``upsert_user`` was rewritten to close exists only where two
connections can hold write transactions open at once, so SQLite cannot
reach it at all — which is why this sits beside the advisory-lock suite
rather than in ``tests/auth``.

The old select-then-insert body loses like this: the second session's
``SELECT`` sees no row, its ``INSERT`` blocks on the unique index, and the
moment the first session commits it is handed a ``UniqueViolation``. That
is the 500 on the loser's OAuth callback. ``ON CONFLICT ... DO UPDATE``
waits on the same lock and then updates and returns the winner's row.

Blocking is asserted against ``pg_stat_activity`` rather than against a
sleep: a thread that has not reached its statement yet is also "still
running", and a timing-only assertion cannot tell the two apart.
"""

from __future__ import annotations

import threading
import time

from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.auth.github import GitHubProfile
from app.auth.users import upsert_user
from app.db.models import User
from tests.postgres.conftest import pytestmark as _pytestmark

pytestmark = _pytestmark

GITHUB_ID = 4242424

#: Long enough to cover connection setup on a cold pool; the assertion is
#: on the server's view of the backend, so this only bounds the wait.
BLOCKED_WITHIN_SECONDS = 10.0
UNBLOCKED_WITHIN_SECONDS = 15.0


def _profile(login: str) -> GitHubProfile:
    return GitHubProfile(
        github_id=GITHUB_ID, login=login, display_name=login.title(), avatar_url=None
    )


def _wait_for_a_lock_wait(engine: Engine, deadline: float) -> bool:
    """Poll until a backend is parked on a lock, as the server sees it."""
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting = connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND state = 'active'"
                )
            ).scalar_one()
        if waiting:
            return True
        time.sleep(0.05)
    return False


def test_the_loser_of_a_first_sign_in_race_reads_the_winners_row(
    pg_clean: Engine, pg_session_factory: sessionmaker[Session]
) -> None:
    winner = pg_session_factory()
    winner.add(User(github_id=GITHUB_ID, github_login="octocat"))
    # Written and therefore locked, deliberately not committed: this is
    # the state the second sign-in has to survive.
    winner.flush()
    winner_id = winner.scalars(select(User.id).where(User.github_id == GITHUB_ID)).one()

    returned: list[int] = []
    failed: list[BaseException] = []

    def racing_sign_in() -> None:
        try:
            with pg_session_factory() as db:
                returned.append(upsert_user(db, _profile("hubot")).id)
                db.commit()
        except BaseException as exc:
            # Caught rather than raised: an exception in a thread would
            # otherwise print and vanish, and the assertions below are
            # where it has to be visible.
            failed.append(exc)

    thread = threading.Thread(target=racing_sign_in, daemon=True)
    thread.start()
    try:
        blocked = _wait_for_a_lock_wait(pg_clean, time.monotonic() + BLOCKED_WITHIN_SECONDS)
        assert blocked, "the racing upsert never waited on the uncommitted row's lock"
        assert thread.is_alive()
        assert not returned
        assert not failed
    finally:
        # Always release, even on a failed assertion: an abandoned lock
        # would hang the next test's TRUNCATE rather than fail it.
        winner.commit()
        winner.close()

    thread.join(timeout=UNBLOCKED_WITHIN_SECONDS)

    assert not thread.is_alive(), "the racing upsert never unblocked"
    assert not failed, f"the racing sign-in raised {failed[0]!r}"
    assert returned == [winner_id], "the loser did not read back the winner's row"

    with pg_session_factory() as check:
        assert check.scalar(select(func.count()).select_from(User)) == 1
        # The loser's profile won, because DO UPDATE applied it.
        assert check.scalars(select(User.github_login)).one() == "hubot"
