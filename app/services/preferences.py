"""Preference profile service — the seam between agents A and C.

Agent A owns ``app/api/v1/me.py`` and calls these functions; agent C owns
this module and implements them. The signatures below are frozen: neither
agent changes them without the coordinator, because both sides are written
in parallel against this contract.

Every function participates in the caller's transaction and must not
commit; the request-scoped session dependency owns commit and rollback.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.api.v1.schemas import PreferencesOut, PreferencesPatch
from app.db.models import User

_UNIMPLEMENTED = "Phase 1 agent C implements this."


def ensure_profile(db: Session, user: User) -> None:
    """Create the instance-default profile if the user has none.

    Idempotent: safe to call on every sign-in. Agent A calls this after
    creating or updating the user record at OAuth callback.

    Defaults must not enable every source at once — see the volume
    asymmetry note in PLAN-v1.md.
    """
    raise NotImplementedError(_UNIMPLEMENTED)


def load_profile(db: Session, user: User) -> PreferencesOut:
    """Return the user's profile, with topic and source **slugs**.

    Assumes :func:`ensure_profile` has run for this user.
    """
    raise NotImplementedError(_UNIMPLEMENTED)


def apply_patch(db: Session, user: User, patch: PreferencesPatch) -> PreferencesOut:
    """Apply a partial update and return the resulting profile.

    Absent fields are untouched; an explicit empty list clears that
    selection. Unknown or disabled topic/source slugs are rejected with
    ``ValueError`` — agent A maps that to HTTP 422.
    """
    raise NotImplementedError(_UNIMPLEMENTED)
