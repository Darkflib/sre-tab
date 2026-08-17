"""Auth routes: GitHub OAuth start, callback, and logout.

Thin by design — HTTP concerns only (rate limiting, cookies, redirects,
status codes). The decisions live in :mod:`app.auth.flow`.

No JSON bodies on this router: start and callback redirect, logout is 204.
CSRF on logout is enforced structurally by
:class:`app.auth.csrf_middleware.CSRFMiddleware` rather than per-route, so
the guarantee covers every mutating endpoint in the API and not only the
ones that remembered to ask.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.v1.schemas import ErrorResponse
from app.auth.flow import SignInDenied, complete_sign_in, start_authorization
from app.auth.ratelimit import SlidingWindowLimiter
from app.auth.sessions import (
    clear_csrf_cookie,
    clear_session_cookie,
    clear_state_cookie,
    issue_csrf_cookie,
    revoke_session,
    set_session_cookie,
    set_state_cookie,
)
from app.auth.state import STATE_COOKIE_NAME
from app.db.session import get_db
from app.settings import Settings

router = APIRouter(prefix="/auth", tags=["auth"])

log = structlog.get_logger(__name__)

# In-process and single-instance by contract (AGENTS.md). Initiation is
# throttled outright; on the callback only *failures* count, so a busy
# instance signing people in successfully is never throttled while a
# grinder against `state` or `code` is.
start_limiter = SlidingWindowLimiter(limit=20, window_seconds=300)
callback_failure_limiter = SlidingWindowLimiter(limit=10, window_seconds=900)

_TOO_MANY_REQUESTS = HTTPException(
    status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many sign-in attempts; try again shortly."
)


def _client_ip(request: Request) -> str:
    """Peer address. Behind a reverse proxy this is the proxy unless the
    server is run with proxy-header handling enabled; no header is trusted
    here, because an untrusted ``X-Forwarded-For`` would let a caller pick
    its own rate-limit bucket."""
    client = request.client
    return client.host if client else "unknown"


@router.get(
    "/github/start",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses={302: {"description": "Redirect to GitHub's authorization page"}},
)
def github_start(request: Request) -> RedirectResponse:
    """Initiate the GitHub OAuth authorization-code flow."""
    settings: Settings = request.app.state.settings
    if not start_limiter.hit(_client_ip(request)):
        raise _TOO_MANY_REQUESTS

    authorization = start_authorization(settings)
    response = RedirectResponse(authorization.url, status_code=status.HTTP_302_FOUND)
    set_state_cookie(response, authorization.state, authorization.ttl_seconds)
    log.info("oauth_start_issued")
    return response


@router.get(
    "/github/callback",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses={
        302: {"description": "Session created; redirect into the application"},
        403: {"model": ErrorResponse, "description": "State mismatch or user not allow-listed"},
    },
)
def github_callback(
    request: Request,
    code: str,
    state: str,
    db: Annotated[Session, Depends(get_db)],
) -> RedirectResponse:
    """Complete OAuth: validate state, exchange the code server-side, and
    issue the session cookie. The code and token never reach logs or
    browser code."""
    settings: Settings = request.app.state.settings
    client_ip = _client_ip(request)
    if callback_failure_limiter.is_limited(client_ip):
        raise _TOO_MANY_REQUESTS

    try:
        _user, token = complete_sign_in(
            db,
            settings,
            code=code,
            state=state,
            state_cookie=request.cookies.get(STATE_COOKIE_NAME),
            current_session_token=request.cookies.get(settings.session_cookie_name),
        )
    except SignInDenied as denied:
        db.rollback()
        callback_failure_limiter.hit(client_ip)
        log.warning("oauth_callback_denied", reason=denied.reason)
        raise HTTPException(denied.status_code, detail=denied.detail) from denied

    db.commit()

    response = RedirectResponse(
        settings.app_base_url.rstrip("/") + "/", status_code=status.HTTP_302_FOUND
    )
    set_session_cookie(response, token, settings)
    issue_csrf_cookie(response, settings)
    clear_state_cookie(response)
    return response


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={403: {"model": ErrorResponse, "description": "CSRF validation failed"}},
)
def logout(request: Request, db: Annotated[Session, Depends(get_db)]) -> Response:
    """Revoke the current session and clear the cookie."""
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.session_cookie_name)
    if token and revoke_session(db, token):
        db.commit()
        log.info("sign_out")

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, settings)
    clear_csrf_cookie(response, settings)
    return response
