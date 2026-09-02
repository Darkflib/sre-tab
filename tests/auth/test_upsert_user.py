"""``upsert_user`` on the create path and the refresh path.

The race this function was rewritten for needs two connections holding
write transactions open at once, which SQLite cannot do — that test lives
in ``tests/postgres/test_signin_race.py``. What is left here is the
everyday behaviour SQLite proves perfectly well: one row per GitHub
account, mutable fields refreshed in place, and ``updated_at`` actually
moving when they are.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, inspect, select, update
from sqlalchemy.orm import Session

from app.auth.github import GitHubProfile
from app.auth.users import upsert_user
from app.db.models import User

GITHUB_ID = 1000001

#: Far enough back that no clock resolution can confuse it with "now".
BACK_DATED = datetime(2020, 1, 1, tzinfo=UTC)


def profile(
    *,
    login: str = "octocat",
    display_name: str | None = "Octo Cat",
    avatar_url: str | None = "https://avatars.example/octocat.png",
) -> GitHubProfile:
    return GitHubProfile(
        github_id=GITHUB_ID, login=login, display_name=display_name, avatar_url=avatar_url
    )


def _rows(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User)) or 0


def test_a_first_sign_in_creates_the_row(db_session: Session) -> None:
    user = upsert_user(db_session, profile())

    assert user.github_id == GITHUB_ID
    assert user.github_login == "octocat"
    assert user.display_name == "Octo Cat"
    assert user.avatar_url == "https://avatars.example/octocat.png"
    assert _rows(db_session) == 1


def test_the_returned_user_is_a_live_orm_instance(db_session: Session) -> None:
    """``complete_sign_in`` hands this straight to ``ensure_profile`` and
    ``create_session``, so a pending or detached instance would be a bug
    that only showed up in the OAuth callback.

    ``user`` is bound rather than inlined into ``inspect()`` on purpose:
    the identity map holds objects weakly, so an unreferenced instance is
    collected between the two calls and its state reports detached.
    """
    user = upsert_user(db_session, profile())
    state = inspect(user)

    assert state.persistent
    assert not state.modified
    assert db_session.get(User, user.id) is user


def test_a_second_sign_in_updates_in_place(db_session: Session) -> None:
    """Acceptance criterion 1: one row per GitHub account, whatever the
    login name does."""
    first = upsert_user(db_session, profile())
    first_id = first.id

    second = upsert_user(
        db_session,
        profile(
            login="hubot",
            display_name="Hubot",
            avatar_url="https://avatars.example/hubot.png",
        ),
    )

    assert second.id == first_id
    assert second.github_login == "hubot"
    assert second.display_name == "Hubot"
    assert second.avatar_url == "https://avatars.example/hubot.png"
    assert _rows(db_session) == 1


def test_a_profile_field_cleared_at_github_is_cleared_here(db_session: Session) -> None:
    """A user who removes their name or avatar upstream expects it gone,
    so the update mapping carries ``None`` rather than dropping it."""
    upsert_user(db_session, profile())
    cleared = upsert_user(db_session, profile(display_name=None, avatar_url=None))

    assert cleared.display_name is None
    assert cleared.avatar_url is None
    assert _rows(db_session) == 1


def test_updated_at_moves_on_a_profile_refresh(db_session: Session) -> None:
    """The guard on the finding that made this rewrite delicate.

    ``users.updated_at`` carries ``onupdate=func.now()``, which is an
    ORM-flush hook — SQLAlchemy does not fold it into a hand-written
    ``on_conflict_do_update`` set clause. Drop ``updated_at`` from
    ``upsert_user``'s update mapping and the column silently freezes at
    its insert value, and every other assertion in this file still
    passes.

    Back-dating rather than comparing two timestamps taken moments
    apart: SQLite's ``CURRENT_TIMESTAMP`` has one-second resolution, so
    two upserts in the same second are indistinguishable and the naive
    version of this test would pass against the broken implementation.
    """
    user = upsert_user(db_session, profile())
    db_session.execute(update(User).where(User.id == user.id).values(updated_at=BACK_DATED))
    db_session.flush()
    stale = db_session.scalar(select(User.updated_at).where(User.id == user.id))

    refreshed = upsert_user(db_session, profile(login="hubot"))

    assert stale is not None
    assert refreshed.updated_at > stale
