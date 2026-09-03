"""Bearer authentication for API tokens, and scope enforcement.

Both live in middleware, and both are here for the reason
:mod:`app.auth.csrf_middleware` gives about CSRF: a rule enforced by a
dependency protects whichever routes remember to ask for it. Scope is
exactly that shape of rule. A read-only token must be refused on *every*
mutating route, including one added next year by somebody who has never
read this file, and the only way to have that be true is for no route to
be consulted about it.

So the middleware answers two questions before the router sees the
request, and the router cannot answer either one differently:

*Who is calling?* An ``Authorization: Bearer`` header naming a live
token, whose owner is still on the allow-list. The resolved identity is
left on ``request.state.api_token`` and :func:`app.api.deps.get_current_user`
picks it up from there, so the credential is resolved once per request
rather than once per dependency.

*May they do this?* A ``READ`` token may not use a mutating method. The
refusal is a 403 emitted here, before the route function exists.

Three narrower decisions, each of which could reasonably have gone the
other way:

**The cookie wins when both are present.** A request carrying the session
cookie is not bearer-authenticated at all — this middleware returns
before it looks at the header. That keeps one rule true with no
exceptions: *CSRF applies exactly when the session cookie is present*.
Were it the other way round, adding an ``Authorization`` header to a
browser request would be a way to skip the CSRF check, which is a hole
worth more than the convenience of letting a stale cookie jar be ignored.
The cost is a curl session that has both credentials and is confused
about which one it is using; the answer is to send one.

**The allow-list is re-checked on every request, not only at sign-in.**
``allowlist.is_authorised`` ran once, in the OAuth callback, because that
was the only moment authorisation was decided. A token outlives that
moment by design — that is what long-lived means — so an account removed
from ``ALLOWED_GITHUB_IDS`` would keep a working credential, which is a
way back in for the one person the operator has just decided to remove.
It fails closed the way that module's doctrine requires: an unset
allow-list denies everyone, tokens included.

**A failed authentication is not answered here.** Unknown, malformed,
revoked, expired, and no-longer-allow-listed all leave
``request.state`` unset and fall through to ``get_current_user``, which
answers the same 401 it answers a browser with no cookie. Nothing about
which of the five it was is useful to a caller, and routing them all
through one refusal is what makes that true by construction rather than
by five matching ``raise`` statements.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker as SessionMaker
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from app.auth import allowlist
from app.auth.api_tokens import resolve_token, touch_last_used
from app.auth.csrf_middleware import UNSAFE_METHODS
from app.db.models import ApiTokenScope, User
from app.settings import Settings

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

log = structlog.get_logger(__name__)

#: The ASGI scope key the resolved identity is left under. Reached as
#: ``request.state.api_token`` from either side — Starlette backs
#: ``Request.state`` with ``scope["state"]``, so the middleware and the
#: route see one dictionary.
STATE_ATTRIBUTE = "api_token"

_SCHEME = "bearer"

_INSUFFICIENT_SCOPE = "This API token is read-only. Create a full-access token to make changes."


@dataclass(frozen=True)
class BearerIdentity:
    """What survives token resolution.

    Ids and a scope, not the ORM objects. The middleware's session is
    closed before the route runs, so handing on a ``User`` would hand on
    a detached instance; the route's own session loads it back by primary
    key instead, which is one indexed lookup and no expired-attribute
    surprises.
    """

    token_id: int
    user_id: int
    scope: ApiTokenScope


def presented_credential(authorization: str) -> str | None:
    """The credential out of an ``Authorization`` header, or None.

    Case-insensitive on the scheme, as RFC 9110 requires. Anything that
    is not exactly a scheme and a non-empty credential is None, which the
    caller treats as "no bearer credential offered" — indistinguishable,
    from outside, from a credential that turned out to be unknown.
    """
    scheme, separator, credential = authorization.partition(" ")
    if not separator or scheme.lower() != _SCHEME:
        return None
    credential = credential.strip()
    return credential or None


def authenticate(db: Session, presented: str, settings: Settings) -> BearerIdentity | None:
    """Resolve a presented token to an identity, or None.

    Records ``last_used_at`` on success — before the scope decision the
    caller then makes, because the token *was* used, and a read-only
    token refused on a mutating route is exactly the sort of thing an
    owner should be able to see the timestamp for.

    Does not commit: the caller opened the session.
    """
    token = resolve_token(db, presented)
    if token is None:
        return None
    user = db.get(User, token.user_id)
    if user is None:  # pragma: no cover - ondelete=CASCADE removes the row with the user
        return None
    if not allowlist.is_authorised(user.github_id, settings):
        log.warning("api_token_denied_not_allow_listed", user_id=user.id, token_id=token.id)
        return None
    touch_last_used(db, token.id)
    return BearerIdentity(token_id=token.id, user_id=user.id, scope=token.scope)


class ApiTokenMiddleware:
    """Resolve bearer credentials and enforce token scope."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        settings: Settings = request.app.state.settings

        # The cookie wins — see the module docstring. This is also what
        # keeps CSRFMiddleware's rule exact rather than approximately
        # right, so the two must not be allowed to drift apart.
        if settings.session_cookie_name in request.cookies:
            await self.app(scope, receive, send)
            return

        presented = presented_credential(request.headers.get("authorization", ""))
        if presented is None:
            await self.app(scope, receive, send)
            return

        identity = await run_in_threadpool(self._authenticate, request, settings, presented)
        if identity is None:
            # Falls through to get_current_user's 401. Deliberate: see
            # the module docstring on refusing all five failures alike.
            await self.app(scope, receive, send)
            return

        setattr(request.state, STATE_ATTRIBUTE, identity)

        if identity.scope is ApiTokenScope.READ and scope.get("method", "") in UNSAFE_METHODS:
            log.info(
                "api_token_scope_refused",
                token_id=identity.token_id,
                user_id=identity.user_id,
                method=scope.get("method"),
            )
            response = JSONResponse(
                {"detail": _INSUFFICIENT_SCOPE}, status_code=status.HTTP_403_FORBIDDEN
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    @staticmethod
    def _authenticate(
        request: Request, settings: Settings, presented: str
    ) -> BearerIdentity | None:
        """The database half, on a worker thread.

        Off the event loop deliberately. The data layer is sync by
        contract, and FastAPI already offloads sync *route* functions to
        a threadpool — but middleware runs on the loop, so a blocking
        query here would stall every other request in flight rather than
        only this one.

        This middleware opens the session, so this middleware commits it
        (AGENTS.md, "Transactions"). The only write is ``last_used_at``.
        """
        factory: SessionMaker[Session] = request.app.state.session_factory
        with factory() as session:
            identity = authenticate(session, presented, settings)
            if identity is not None:
                session.commit()
        return identity
