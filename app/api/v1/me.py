"""Current-user routes.

Thin handlers: the profile work belongs to ``app.services.preferences``
(agent C), and account deletion is one statement leaning on the schema's
``ondelete="CASCADE"``.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.api.v1.schemas import ErrorResponse, MeResponse, PreferencesOut, PreferencesPatch, UserOut
from app.auth.sessions import clear_csrf_cookie, clear_session_cookie
from app.db.models import User
from app.db.session import get_db
from app.services import preferences
from app.settings import Settings

router = APIRouter(prefix="/me", tags=["me"])

log = structlog.get_logger(__name__)

_UNAUTHENTICATED: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Not signed in"}
}


@router.get("", response_model=MeResponse, responses=_UNAUTHENTICATED)
def get_me(user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> MeResponse:
    """Current user and their preference profile."""
    return MeResponse(
        user=UserOut.model_validate(user),
        preferences=preferences.load_profile(db, user),
    )


@router.patch("/preferences", response_model=PreferencesOut, responses=_UNAUTHENTICATED)
def patch_preferences(
    user: CurrentUser,
    patch: PreferencesPatch,
    db: Annotated[Session, Depends(get_db)],
) -> PreferencesOut:
    """Partial preference update in one transaction."""
    try:
        profile = preferences.apply_patch(db, user, patch)
    except ValueError as exc:
        # Unknown or disabled topic/source slugs: the service's contract.
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    db.commit()
    return profile


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, responses=_UNAUTHENTICATED)
def delete_me(
    user: CurrentUser,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Delete the account and cascade preferences, bookmarks, reads, and
    sessions; then sign out."""
    settings: Settings = request.app.state.settings
    user_id = user.id

    # One statement: every user-owned foreign key is ondelete="CASCADE", so
    # the database removes preferences, topic/source selections, sessions,
    # read state, and bookmarks. SQLite only honours that with
    # PRAGMA foreign_keys=ON, which app.db.engine sets on connect — the
    # cascade is asserted in tests rather than assumed.
    db.execute(delete(User).where(User.id == user_id))
    db.commit()
    log.info("account_deleted", user_id=user_id)

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, settings)
    clear_csrf_cookie(response, settings)
    return response
