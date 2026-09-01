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

import structlog
from fastapi import Response
from sqlalchemy import CursorResult, and_, delete, or_, select, update
from sqlalchemy.orm import Session

from app.auth.state import STATE_COOKIE_NAME, STATE_COOKIE_PATH
from app.db.models import User, UserSession
from app.security.csrf import generate_csrf_token
from app.security.tokens import generate_session_token, hash_session_token
from app.settings import Settings

log = structlog.get_logger(__name__)

COOKIE_PATH = "/"

#: How long a revoked session row is kept after its ``revoked_at``. See
#: :func:`prune_sessions` for why this is not zero.
REVOKED_RETENTION_DAYS = 7


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


def prune_sessions(
    db: Session,
    *,
    now: datetime | None = None,
    revoked_retention_days: int = REVOKED_RETENTION_DAYS,
) -> int:
    """Delete session rows that are dead. Returns the number removed.

    Nothing else ever deletes from this table — ``create_session`` inserts,
    logout sets ``revoked_at``, and every read filters — so without this
    sweep the table grows by one row per sign-in for the life of the
    deployment. Rotation makes that worse than it sounds: signing in
    revokes the previous session and adds a second row, so an ordinary
    user contributes rows at the rate they open the app, not the rate they
    forget to log out.

    Two classes of row are dead, and they are not dead at the same moment:

    *Expired and never revoked.* Gone as soon as ``expires_at`` passes.
    The row cannot authenticate anything and records nothing an operator
    would ask about — its whole content is "a session existed and ran out",
    which is the unremarkable case.

    *Revoked.* Kept for ``revoked_retention_days`` after ``revoked_at``,
    not deleted on the spot. The revocation timestamp is the only trace
    this system keeps that a logout — or a token rotation, which revokes
    the same way — ever happened. Deleting it the instant the user signs
    out means that "when did this session end, and did it end deliberately
    or by expiry?" has no answer during the week when someone is most
    likely to ask, which is the week after a suspected account compromise.
    A revoked row is inert either way: :func:`resolve_session` refuses it
    on ``revoked_at IS NULL``, so retaining it grants nothing. Seven days
    is chosen to be shorter than the default ``session_ttl_days`` (14), so
    the grace window never keeps a row longer than leaving it alone would
    have.

    Note the interaction: a row that is *both* revoked and expired is held
    by the grace window, not swept by the expiry branch. The forensic
    value is in the revocation, and it does not stop mattering because the
    session would have timed out anyway a minute later.

    Takes a session and does not commit — the caller owns the transaction
    (AGENTS.md, "Transactions").
    """
    moment = now or datetime.now(UTC)
    revoked_cutoff = moment - timedelta(days=revoked_retention_days)

    # Both branches are evaluated in SQL, for the reason in the module
    # docstring: SQLite hands back naive datetimes for these columns, so a
    # Python-side comparison against an aware ``moment`` compares an aware
    # value with a naive one and raises — or, worse on a future engine,
    # quietly compares wall clocks across zones. Binding ``moment`` as a
    # parameter and letting the database do the comparison is what keeps
    # this identical on SQLite and PostgreSQL.
    #
    # ``revoked_at <= revoked_cutoff`` already excludes live sessions
    # without an explicit NOT NULL: comparing NULL yields NULL, which is
    # not true, so an unrevoked row can only ever be reached by the first
    # branch — where it must also have expired.
    dead = or_(
        and_(UserSession.revoked_at.is_(None), UserSession.expires_at <= moment),
        UserSession.revoked_at <= revoked_cutoff,
    )

    # synchronize_session=False for the same reason as
    # ``app.ingest.store.prune_feed_items``: this is a bulk delete, and the
    # ORM's default in-Python evaluation of the criterion would reintroduce
    # exactly the aware/naive comparison the SQL predicate exists to avoid.
    statement = delete(UserSession).where(dead).execution_options(synchronize_session=False)
    result = cast("CursorResult[Any]", db.execute(statement))
    removed = result.rowcount or 0
    if removed:
        log.info(
            "sessions_pruned",
            removed=removed,
            revoked_cutoff=revoked_cutoff.isoformat(),
            expired_before=moment.isoformat(),
        )
    return removed


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_days * 24 * 60 * 60,
        path=COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        path=COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def issue_csrf_cookie(response: Response, settings: Settings, session_token: str) -> str:
    """Set the CSRF cookie alongside a new session.

    Deliberately *not* ``HttpOnly``: the double-submit pattern requires the
    frontend to read this value and echo it in the CSRF header. Secrecy is
    therefore not available as a defence and is not relied upon — the token
    is bound to ``session_token``, so it verifies against that session and
    no other, and one lifted from elsewhere is inert here.
    """
    token = generate_csrf_token(settings.session_secret.get_secret_value(), session_token)
    response.set_cookie(
        settings.csrf_cookie_name,
        token,
        max_age=settings.session_ttl_days * 24 * 60 * 60,
        path=COOKIE_PATH,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    return token


def clear_csrf_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.csrf_cookie_name,
        path=COOKIE_PATH,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def set_state_cookie(response: Response, state: str, ttl_seconds: int, settings: Settings) -> None:
    """Bind the OAuth state to this browser for the length of the flow."""
    response.set_cookie(
        STATE_COOKIE_NAME,
        state,
        max_age=ttl_seconds,
        path=STATE_COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def clear_state_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        STATE_COOKIE_NAME,
        path=STATE_COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
