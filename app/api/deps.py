"""Shared API dependencies.

``get_current_user`` ships with its final signature but raises 501 until
Phase 1 agent A supplies the body (session cookie -> token hash lookup ->
unexpired, unrevoked session -> user). Everyone else imports and depends
on it as-is; tests override it via the ``authed_client`` fixture.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db.models import User
from app.db.session import get_db


def get_current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> User:
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Authentication not implemented — Phase 1 (agent A)",
    )


CurrentUser = Annotated[User, Depends(get_current_user)]
