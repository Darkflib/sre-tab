"""Item read-state route — Phase 1 agent C replaces the body; contract
fixed."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser
from app.api.v1.schemas import ErrorResponse, ReadStateOut, ReadStateUpdate

router = APIRouter(prefix="/items", tags=["items"])

_NOT_IMPLEMENTED = HTTPException(
    status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented — Phase 1 (agent C)"
)

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Not signed in"},
    404: {"model": ErrorResponse, "description": "Unknown item"},
}


@router.put("/{item_id}/read-state", response_model=ReadStateOut, responses=_ERRORS)
def put_read_state(user: CurrentUser, item_id: int, update: ReadStateUpdate) -> ReadStateOut:
    """Mark an item read or unread; idempotent either way."""
    raise _NOT_IMPLEMENTED
