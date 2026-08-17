"""Current-user routes — Phase 1 agent A replaces the bodies; contracts
are fixed here."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Response, status

from app.api.deps import CurrentUser
from app.api.v1.schemas import ErrorResponse, MeResponse, PreferencesOut, PreferencesPatch

router = APIRouter(prefix="/me", tags=["me"])

_NOT_IMPLEMENTED = HTTPException(
    status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented — Phase 1 (agent A)"
)

_UNAUTHENTICATED: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Not signed in"}
}


@router.get("", response_model=MeResponse, responses=_UNAUTHENTICATED)
def get_me(user: CurrentUser) -> MeResponse:
    """Current user and their preference profile."""
    raise _NOT_IMPLEMENTED


@router.patch("/preferences", response_model=PreferencesOut, responses=_UNAUTHENTICATED)
def patch_preferences(user: CurrentUser, patch: PreferencesPatch) -> PreferencesOut:
    """Partial preference update in one transaction."""
    raise _NOT_IMPLEMENTED


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, responses=_UNAUTHENTICATED)
def delete_me(user: CurrentUser) -> Response:
    """Delete the account and cascade preferences, bookmarks, reads, and
    sessions; then sign out."""
    raise _NOT_IMPLEMENTED
