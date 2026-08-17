"""CSRF protection: session-bound double-submit cookie.

Design choice (documented per the Phase 0 brief): the token is a random
payload plus an HMAC-SHA256 signature under ``SESSION_SECRET`` *over the
session it was issued for*, delivered in a cookie the frontend echoes
back in the ``X-CSRF-Token`` header on every mutating request. Stdlib
only (hmac + secrets + hashlib), by design.

What the signature buys, precisely. A bare double-submit cookie proves
only that the same value reached the server twice, so anything able to
*write* a cookie — a sibling subdomain, an active network attacker on a
plain-http origin — can supply both halves. Signing on its own does not
close that: an attacker who can obtain any validly signed token, by
signing in themselves, could pair it with the victim's session. The
binding is what closes it. The token commits to
``sha256(session_token)``, the same digest the ``sessions`` row stores,
so a token minted for one session verifies against that session and no
other. An injected cookie is now useless unless the attacker already
knows the victim's session token — at which point CSRF is moot.

The cookie is deliberately readable by script (see
``issue_csrf_cookie``): the frontend has to read it to echo it. Secrecy
was never the property; the binding is.

Agent A issues the cookie at session creation; mutating routes attach
``Depends(require_csrf)``, and
:class:`app.auth.csrf_middleware.CSRFMiddleware` enforces it for every
mutating route regardless.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request, status

from app.security.tokens import compare_secret, hash_session_token
from app.settings import Settings

_SEPARATOR = "."


def _sign(payload: str, secret: str, session_binding: str) -> str:
    message = f"{payload}{_SEPARATOR}{session_binding}"
    return hmac.new(secret.encode(), message.encode(), "sha256").hexdigest()


def generate_csrf_token(secret: str, session_token: str) -> str:
    """Mint a token that is valid only for ``session_token``'s session."""
    payload = secrets.token_urlsafe(16)
    return f"{payload}{_SEPARATOR}{_sign(payload, secret, hash_session_token(session_token))}"


def validate_csrf_token(token: str, secret: str, session_token: str) -> bool:
    """True when ``token`` was minted by us *for this session*."""
    payload, separator, signature = token.partition(_SEPARATOR)
    if not separator or not payload:
        return False
    return compare_secret(signature, _sign(payload, secret, hash_session_token(session_token)))


def require_csrf(request: Request) -> None:
    """Dependency for mutating routes: cookie and header must both be
    present, identical, and validly signed for the presented session."""
    settings: Settings = request.app.state.settings
    session_token = request.cookies.get(settings.session_cookie_name, "")
    cookie = request.cookies.get(settings.csrf_cookie_name, "")
    header = request.headers.get(settings.csrf_header_name, "")
    if (
        not session_token
        or not cookie
        or not header
        or not compare_secret(cookie, header)
        or not validate_csrf_token(
            cookie, settings.session_secret.get_secret_value(), session_token
        )
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")
