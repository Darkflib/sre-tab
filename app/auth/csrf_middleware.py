"""Application-wide CSRF enforcement.

The Phase 0 contract offers ``Depends(require_csrf)`` per route. That is
the wrong granularity for a guarantee: it protects whichever routes
remember to ask for it, and Phase 1 fans out across five agents who each
own different route modules. Enforcing in middleware instead makes the
property structural — every mutating request on every router is checked,
including agent C's read-state and bookmark endpoints, without editing a
line agent C owns.

Two deliberate narrowings:

*Method* — only ``POST``/``PATCH``/``PUT``/``DELETE``. ``GET`` is not
state-changing, and the OAuth callback is a top-level cross-site
navigation that must be allowed to arrive without a CSRF header.

*Credential* — only when the request carries the session cookie. CSRF is an
attack on *ambient authority*: it works because the browser attaches the
session cookie to a forged cross-site request. A mutating request without
that cookie has no authority to abuse and is answered 401 by
``get_current_user`` regardless. Gating this way also means the check never
fires on unauthenticated traffic, which would turn a 401 into a confusing
403.

That second narrowing is what exempts an API token, and the exemption is
exact rather than approximate. A bearer credential is a header, so no
browser attaches it to a cross-site request on its own and there is no
ambient authority to forge; requiring a CSRF token alongside it would also
make the API unusable from the other application it exists for, since that
application has no cookie jar to read one out of. The condition stays
"carries the session cookie" — not "carries no ``Authorization`` header" —
because :class:`app.auth.bearer.ApiTokenMiddleware` refuses to
bearer-authenticate a request that has the cookie. Cookie present therefore
means *the cookie is what authenticates*, in both files, with no third
state; adding an ``Authorization`` header to a browser request cannot skip
this check, it is simply ignored. ``tests/auth/test_api_token_csrf.py``
pins both directions and the both-credentials case.

Ordering matters: this middleware is registered *innermost*, so a rejection
still passes back out through the request-ID and security-header
middleware and carries the same headers as any other response.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.security.csrf import require_csrf
from app.settings import Settings

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

UNSAFE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


class CSRFMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "") not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        settings: Settings = request.app.state.settings
        if settings.session_cookie_name not in request.cookies:
            await self.app(scope, receive, send)
            return

        try:
            require_csrf(request)
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
