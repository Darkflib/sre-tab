"""Server-side sessions and the cookies that carry them.

Only the SHA-256 digest of a session token is stored (``app.security.tokens``);
the raw token exists in the cookie and nowhere else, so a database leak
yields nothing usable. Revocation and expiry are both evaluated in SQL, in
one statement, which keeps the check honest across SQLite and PostgreSQL —
SQLite hands back naive datetimes for ``DateTime(timezone=True)`` columns,
so a Python-side comparison against an aware ``now`` would be a portability
trap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import Response
from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from app.auth.state import STATE_COOKIE_NAME, STATE_COOKIE_PATH
from app.db.models import User, UserSession
from app.security.csrf import generate_csrf_token
from app.security.tokens import generate_session_token, hash_session_token
from app.settings import Settings

COOKIE_PATH = "/"


def create_session(db: Session, user: User, settings: Settings) -> str:
    """Issue a session for ``user`` and return the raw token.

    Participates in the caller's transaction: the row is flushed, never
    committed here.
    """
    token = generate_session_token()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_session_token(token),
            expires_at=datetime.now(UTC) + timedelta(days=settings.session_ttl_days),
        )
    )
    db.flush()
    return token


def resolve_session(db: Session, token: str) -> User | None:
    """The user behind a live session token, or None.

    "Live" means the session exists, is unrevoked, and has not expired —
    all three asserted in the query, so a revoked or expired session can
    never authenticate.
    """
    statement = (
        select(User)
        .join(UserSession, UserSession.user_id == User.id)
        .where(
            UserSession.token_hash == hash_session_token(token),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > datetime.now(UTC),
        )
    )
    return db.execute(statement).scalar_one_or_none()


def revoke_session(db: Session, token: str) -> bool:
    """Mark the session behind ``token`` revoked. True if one was live."""
    result = cast(
        "CursorResult[Any]",
        db.execute(
            update(UserSession)
            .where(
                UserSession.token_hash == hash_session_token(token),
                UserSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        ),
    )
    return bool(result.rowcount)


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_days * 24 * 60 * 60,
        path=COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        path=COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="lax",
    )


def issue_csrf_cookie(response: Response, settings: Settings) -> str:
    """Set the CSRF cookie alongside a new session.

    Deliberately *not* ``HttpOnly``: the double-submit pattern requires the
    frontend to read this value and echo it in the CSRF header. The value
    is signed, so it is useless to script on another origin.
    """
    token = generate_csrf_token(settings.session_secret.get_secret_value())
    response.set_cookie(
        settings.csrf_cookie_name,
        token,
        max_age=settings.session_ttl_days * 24 * 60 * 60,
        path=COOKIE_PATH,
        httponly=False,
        secure=True,
        samesite="lax",
    )
    return token


def clear_csrf_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.csrf_cookie_name,
        path=COOKIE_PATH,
        httponly=False,
        secure=True,
        samesite="lax",
    )


def set_state_cookie(response: Response, state: str, ttl_seconds: int) -> None:
    """Bind the OAuth state to this browser for the length of the flow."""
    response.set_cookie(
        STATE_COOKIE_NAME,
        state,
        max_age=ttl_seconds,
        path=STATE_COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="lax",
    )


def clear_state_cookie(response: Response) -> None:
    response.delete_cookie(
        STATE_COOKIE_NAME,
        path=STATE_COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="lax",
    )
