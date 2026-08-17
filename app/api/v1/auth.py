"""Auth routes — Phase 1 agent A replaces the bodies; paths, parameters,
and response contracts are fixed here.

No JSON bodies on this router: start/callback redirect, logout is 204.
Agent A attaches ``Depends(require_csrf)`` to logout when implementing it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import RedirectResponse

from app.api.v1.schemas import ErrorResponse

router = APIRouter(prefix="/auth", tags=["auth"])

_NOT_IMPLEMENTED = HTTPException(
    status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented — Phase 1 (agent A)"
)


@router.get(
    "/github/start",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses={302: {"description": "Redirect to GitHub's authorization page"}},
)
def github_start() -> RedirectResponse:
    """Initiate the GitHub OAuth authorization-code flow."""
    raise _NOT_IMPLEMENTED


@router.get(
    "/github/callback",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses={
        302: {"description": "Session created; redirect into the application"},
        403: {"model": ErrorResponse, "description": "State mismatch or user not allow-listed"},
    },
)
def github_callback(code: str, state: str) -> RedirectResponse:
    """Complete OAuth: validate state, exchange the code server-side, and
    issue the session cookie. The code and token never reach logs or
    browser code."""
    raise _NOT_IMPLEMENTED


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={403: {"model": ErrorResponse, "description": "CSRF validation failed"}},
)
def logout() -> Response:
    """Revoke the current session and clear the cookie."""
    raise _NOT_IMPLEMENTED
