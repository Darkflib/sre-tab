"""User records keyed on GitHub's stable numeric ID.

Acceptance criterion 1 lives here: the same GitHub account must never
produce a second ``users`` row, however often the login name, display name,
or avatar changes. ``github_id`` is unique in the schema, so the invariant
is enforced by the database as well as by this lookup.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.github import GitHubProfile
from app.db.models import User


def upsert_user(db: Session, profile: GitHubProfile) -> User:
    """Create the user on first sign-in; refresh mutable profile fields
    on every later one. Flushed, not committed — the caller owns the
    transaction."""
    user = db.execute(select(User).where(User.github_id == profile.github_id)).scalar_one_or_none()

    if user is None:
        user = User(
            github_id=profile.github_id,
            github_login=profile.login,
            display_name=profile.display_name,
            avatar_url=profile.avatar_url,
        )
        db.add(user)
    else:
        user.github_login = profile.login
        user.display_name = profile.display_name
        user.avatar_url = profile.avatar_url

    db.flush()
    db.refresh(user)
    return user
