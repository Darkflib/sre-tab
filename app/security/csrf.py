"""CSRF protection: signed double-submit cookie.

Design choice (documented per the Phase 0 brief): the token is a random
payload plus an HMAC-SHA256 signature under ``SESSION_SECRET``, delivered
in a cookie the frontend echoes back in the ``X-CSRF-Token`` header on
every mutating request. Signing closes the classic double-submit gap — a
subdomain or MITM-injected cookie fails validation because an attacker
cannot produce the signature. Stdlib only (hmac + secrets), by design.

Agent A issues the cookie at session creation; mutating routes attach
``Depends(require_csrf)``.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request, status

from app.settings import Settings

_SEPARATOR = "."


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), "sha256").hexdigest()


def generate_csrf_token(secret: str) -> str:
    payload = secrets.token_urlsafe(16)
    return f"{payload}{_SEPARATOR}{_sign(payload, secret)}"


def validate_csrf_token(token: str, secret: str) -> bool:
    payload, separator, signature = token.partition(_SEPARATOR)
    if not separator or not payload:
        return False
    return hmac.compare_digest(signature, _sign(payload, secret))


def require_csrf(request: Request) -> None:
    """Dependency for mutating routes: cookie and header must both be
    present, identical, and validly signed."""
    settings: Settings = request.app.state.settings
    cookie = request.cookies.get(settings.csrf_cookie_name, "")
    header = request.headers.get(settings.csrf_header_name, "")
    if (
        not cookie
        or not header
        or not hmac.compare_digest(cookie, header)
        or not validate_csrf_token(cookie, settings.session_secret.get_secret_value())
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")
