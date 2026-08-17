"""Bookmark routes — Phase 1 agent C replaces the bodies; contracts
fixed. Bookmark create/remove live under /items/{item_id} per the PRD
endpoint table, so this router declares full paths."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.deps import CurrentUser
from app.api.v1.schemas import BookmarkOut, BookmarkPage, ErrorResponse
from app.api.v1.schemas.feed import FEED_DEFAULT_PAGE_SIZE, FEED_MAX_PAGE_SIZE

router = APIRouter(tags=["bookmarks"])

_NOT_IMPLEMENTED = HTTPException(
    status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented — Phase 1 (agent C)"
)

_UNAUTHENTICATED: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Not signed in"}
}
_ITEM_ERRORS: dict[int | str, dict[str, Any]] = {
    **_UNAUTHENTICATED,
    404: {"model": ErrorResponse, "description": "Unknown item"},
}


@router.get("/bookmarks", response_model=BookmarkPage, responses=_UNAUTHENTICATED)
def list_bookmarks(
    user: CurrentUser,
    cursor: Annotated[str | None, Query(description="Opaque cursor from a previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=FEED_MAX_PAGE_SIZE)] = FEED_DEFAULT_PAGE_SIZE,
) -> BookmarkPage:
    """The current user's bookmarks, newest first."""
    raise _NOT_IMPLEMENTED


@router.put("/items/{item_id}/bookmark", response_model=BookmarkOut, responses=_ITEM_ERRORS)
def put_bookmark(user: CurrentUser, item_id: int) -> BookmarkOut:
    """Create a bookmark idempotently; repeats return the existing one."""
    raise _NOT_IMPLEMENTED


@router.delete(
    "/items/{item_id}/bookmark", status_code=status.HTTP_204_NO_CONTENT, responses=_ITEM_ERRORS
)
def delete_bookmark(user: CurrentUser, item_id: int) -> Response:
    """Remove a bookmark; removing an absent bookmark is a 204 no-op."""
    raise _NOT_IMPLEMENTED
