"""Sources listing — Phase 1 agent C replaces the body; contract fixed."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser
from app.api.v1.schemas import ErrorResponse, SourcesResponse

router = APIRouter(tags=["sources"])

_NOT_IMPLEMENTED = HTTPException(
    status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented — Phase 1 (agent C)"
)


@router.get(
    "/sources",
    response_model=SourcesResponse,
    responses={401: {"model": ErrorResponse, "description": "Not signed in"}},
)
def list_sources(user: CurrentUser) -> SourcesResponse:
    """Enabled sources and topic metadata for the settings screen."""
    raise _NOT_IMPLEMENTED
