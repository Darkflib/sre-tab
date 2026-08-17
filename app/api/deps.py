"""Shared API dependencies.

``get_current_user`` resolves the session cookie to a user: hash the raw
token, find an unrevoked, unexpired session, return its owner. The
signature and the ``CurrentUser`` alias are the Phase 0 contract other
agents compile against; only the body is Phase 1 agent A's. Tests override
it via the ``authed_client`` fixture.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth.sessions import resolve_session
from app.db.models import User
from app.db.session import get_db
from app.settings import Settings

_UNAUTHENTICATED = HTTPException(
    status.HTTP_401_UNAUTHORIZED,
    detail="Not signed in",
)


def get_current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> User:
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise _UNAUTHENTICATED
    user = resolve_session(db, token)
    if user is None:
        # Absent, forged, revoked, and expired all answer the same way:
        # nothing about which it was is useful to a caller.
        raise _UNAUTHENTICATED
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
