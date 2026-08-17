"""Item read-state route."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.api.v1.schemas import ErrorResponse, ReadStateOut, ReadStateUpdate
from app.db.session import get_db
from app.services import read_state as read_state_service
from app.services.errors import ItemNotFoundError

router = APIRouter(prefix="/items", tags=["items"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Not signed in"},
    404: {"model": ErrorResponse, "description": "Unknown item"},
}


@router.put("/{item_id}/read-state", response_model=ReadStateOut, responses=_ERRORS)
def put_read_state(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    item_id: int,
    update: ReadStateUpdate,
) -> ReadStateOut:
    """Mark an item read or unread; idempotent either way."""
    try:
        result = read_state_service.set_read_state(db, user, item_id, read=update.read)
    except ItemNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown item") from exc
    db.commit()
    return result
