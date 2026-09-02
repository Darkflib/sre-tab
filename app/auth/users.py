"""User records keyed on GitHub's stable numeric ID.

Acceptance criterion 1 lives here: the same GitHub account must never
produce a second ``users`` row, however often the login name, display name,
or avatar changes. ``github_id`` is unique in the schema, so the invariant
is enforced by the database as well as by this lookup.

Two concurrent *first* sign-ins for one account are the case a
select-then-insert cannot survive. Both find no row, both insert, and the
unique constraint turns the loser's OAuth callback into a 500 — the table
stays correct and the user gets an error page. So the lookup, the insert,
and the profile refresh are one ``ON CONFLICT (github_id) DO UPDATE ...
RETURNING`` statement: there is no window between reading and writing,
because there is no second statement. ``app.services.upsert`` carries the
reasoning for DO UPDATE over DO NOTHING, which is not the reasoning one
would guess.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.github import GitHubProfile
from app.db.models import User
from app.services.upsert import upsert_returning


def upsert_user(db: Session, profile: GitHubProfile) -> User:
    """Create the user on first sign-in; refresh mutable profile fields
    on every later one. Written and read back in one statement, not
    committed — the caller owns the transaction."""
    mutable = {
        "github_login": profile.login,
        "display_name": profile.display_name,
        "avatar_url": profile.avatar_url,
    }
    return upsert_returning(
        db,
        User,
        {"github_id": profile.github_id, **mutable},
        index_elements=["github_id"],
        # updated_at by hand: the column's onupdate never reaches a DO
        # UPDATE set clause, and nothing here flushes for it to fire on.
        update={**mutable, "updated_at": func.now()},
    )
