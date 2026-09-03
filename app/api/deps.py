"""Shared API dependencies.

``get_current_user`` resolves a request to a user by one of two
credentials. The session cookie is the browser's: hash the raw token,
find an unrevoked, unexpired session, return its owner. The
``Authorization: Bearer`` header is another application's, and by the
time this runs :class:`app.auth.bearer.ApiTokenMiddleware` has already
resolved it, re-checked the allow-list, and enforced the token's scope —
so all that is left here is loading the user the middleware named.

The signature and the ``CurrentUser`` alias are the Phase 0 contract
other agents compile against, and they are unchanged by API tokens: the
new credential arrives through ``request``, which was already a
parameter. Nothing that depends on ``CurrentUser`` needed editing, and
the ``authed_client`` fixture still overrides it the same way.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth.bearer import STATE_ATTRIBUTE, BearerIdentity
from app.auth.sessions import resolve_session
from app.db.models import User
from app.db.session import get_db
from app.settings import Settings


def _unauthenticated() -> HTTPException:
    """A *fresh* 401 every time, never a shared instance.

    Raising one module-global exception object is the tempting shape and
    it leaks without bound: each ``raise`` appends this frame to the
    object's ``__traceback__``, and a module global is never collected,
    so every 401 permanently pins its ``Request``, its raw token, and its
    ``Session``. Measured at 32,719 bytes per request — roughly 23,000
    unauthenticated requests to exhaust the unit's ``MemoryMax=768M``, on
    a route that needs no credentials and has no rate limiter.
    ``tests/auth/test_exception_identity.py`` fails if this reverts.
    """
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="Not signed in",
    )


def _bearer_identity(request: Request) -> BearerIdentity | None:
    identity: BearerIdentity | None = getattr(request.state, STATE_ATTRIBUTE, None)
    return identity


def get_current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> User:
    settings: Settings = request.app.state.settings

    # Only ever one of the two: the middleware declines to bearer-
    # authenticate a request carrying the session cookie, so this branch
    # and the next are mutually exclusive rather than merely ordered.
    identity = _bearer_identity(request)
    if identity is not None:
        user = db.get(User, identity.user_id)
        if user is None:  # pragma: no cover - the middleware just loaded this row
            raise _unauthenticated()
        return user

    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise _unauthenticated()
    user = resolve_session(db, token)
    if user is None:
        # Absent, forged, revoked, and expired all answer the same way:
        # nothing about which it was is useful to a caller. A token that
        # failed to authenticate arrives here too, by the same route and
        # with the same answer.
        raise _unauthenticated()
    return user


def require_interactive_session(request: Request) -> None:
    """Refuse a request authenticated by API token.

    Attached to the token-management router, and to nothing else. The
    reasoning is about what revocation is worth. A ``FULL`` token can
    already do everything the API offers, so letting it also mint and
    revoke tokens costs nothing an attacker did not already have — until
    the moment the leak is noticed, at which point revoking the leaked
    token accomplishes nothing, because whoever holds it has had the
    ability to issue themselves a replacement the whole time. Requiring
    the interactive session for token management is what makes revoking
    a token actually end the access it granted.

    It is a per-router dependency rather than a per-route one on purpose:
    a fourth route added to that router inherits it, and a route added
    anywhere else is not affected, which is the correct scope for a rule
    about one resource.
    """
    if _bearer_identity(request) is not None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="API tokens can only be managed from a signed-in session.",
        )


CurrentUser = Annotated[User, Depends(get_current_user)]
