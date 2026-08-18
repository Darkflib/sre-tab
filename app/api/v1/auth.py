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


def _too_many_requests() -> HTTPException:
    """A *fresh* 429 every time — see ``app.api.deps._unauthenticated``.

    Sharper here than on the 401 path: this is raised at the top of
    ``github_callback``, where ``code`` and ``state`` are already bound,
    so a shared instance pinned real OAuth codes in frame locals for the
    life of the process. That is the same thing the logging rule in
    AGENTS.md forbids, arriving by a different route.
    """
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many sign-in attempts; try again shortly."
    )


def _client_ip(request: Request) -> str:
    """The rate-limit bucket: the peer address, and nothing a caller sent.

    No header is read here on purpose — an ``X-Forwarded-For`` this layer
    trusted would let a caller pick its own bucket, which is the same as
    having no limit. Behind a proxy the real address arrives because
    uvicorn's proxy-header handling rewrote ``scope["client"]`` before
    the request got here, and it only does that for peers named in
    ``FORWARDED_ALLOW_IPS``. That is a trust decision made once, at the
    server boundary, from configuration — not per request, from a header.
    See "TLS termination" in deploy/README.md.
    """
    client = request.client
    return client.host if client else "unknown"


#: Landing-page tokens. Fixed strings chosen here rather than anything
#: GitHub sent: the value ends up in a URL the browser follows, so
#: reflecting an upstream error code would be a reflection bug waiting to
#: be found. The frontend maps these two to English.
_SIGN_IN_CANCELLED = "cancelled"
_SIGN_IN_FAILED = "failed"


def _landing_redirect(settings: Settings, outcome: str) -> RedirectResponse:
    return RedirectResponse(
        f"{settings.app_base_url.rstrip('/')}/?signin={outcome}",
        status_code=status.HTTP_302_FOUND,
    )


@router.get(
    "/github/start",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses={
        302: {"description": "Redirect to GitHub's authorization page"},
        429: {"model": ErrorResponse, "description": "Too many sign-in attempts"},
    },
)
def github_start(request: Request) -> RedirectResponse:
    """Initiate the GitHub OAuth authorization-code flow."""
    settings: Settings = request.app.state.settings
    if not start_limiter.hit(_client_ip(request)):
        raise _too_many_requests()

    authorization = start_authorization(settings)
    response = RedirectResponse(authorization.url, status_code=status.HTTP_302_FOUND)
    set_state_cookie(response, authorization.state, authorization.ttl_seconds, settings)
    log.info("oauth_start_issued")
    return response


@router.get(
    "/github/callback",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses={
        302: {
            "description": (
                "Session created; redirect into the application. Also the "
                "response when GitHub reports a user denial, in which case "
                "the redirect carries a ?signin= outcome and no session."
            )
        },
        403: {"model": ErrorResponse, "description": "State mismatch or user not allow-listed"},
        429: {"model": ErrorResponse, "description": "Too many failed sign-in attempts"},
    },
)
def github_callback(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    """Complete OAuth: validate state, exchange the code server-side, and
    issue the session cookie. The code and token never reach logs or
    browser code.

    Nothing here is a required query parameter, and that is deliberate.
    GitHub redirects a user who clicks "Cancel" to
    ``?error=access_denied&error_description=…&state=…`` with no ``code``
    at all. Declaring ``code`` required turns that ordinary, expected
    outcome into FastAPI's 422 validation error — a wall of JSON where a
    "sign-in was cancelled" message belongs.
    """
    settings: Settings = request.app.state.settings
    client_ip = _client_ip(request)
    if callback_failure_limiter.is_limited(client_ip):
        raise _too_many_requests()

    if error is not None or code is None or state is None:
        # No credential was presented, so there is nothing to verify and
        # nothing to leak; it still counts against the failure budget,
        # because a grinder can reach this branch as easily as a user.
        callback_failure_limiter.hit(client_ip)
        log.info(
            "oauth_callback_declined",
            # GitHub's own error code, logged but never reflected back
            # into the redirect. error_description is free text from
            # upstream and is deliberately not logged verbatim.
            reason=error or ("no_code" if code is None else "no_state"),
            has_description=error_description is not None,
        )
        outcome = _SIGN_IN_CANCELLED if error == "access_denied" else _SIGN_IN_FAILED
        return _landing_redirect(settings, outcome)

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
    issue_csrf_cookie(response, settings, token)
    clear_state_cookie(response, settings)
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
