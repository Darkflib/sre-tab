"""Bookmark routes. Bookmark create/remove live under /items/{item_id}
per the PRD endpoint table, so this router declares full paths."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.api.v1.schemas import BookmarkOut, BookmarkPage, ErrorResponse
from app.api.v1.schemas.feed import FEED_DEFAULT_PAGE_SIZE, FEED_MAX_PAGE_SIZE
from app.db.session import get_db
from app.services import bookmarks as bookmarks_service
from app.services.errors import InvalidCursorError, ItemNotFoundError

router = APIRouter(tags=["bookmarks"])

_UNAUTHENTICATED: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Not signed in"}
}
_LIST_ERRORS: dict[int | str, dict[str, Any]] = {
    **_UNAUTHENTICATED,
    400: {"model": ErrorResponse, "description": "Malformed cursor"},
}
_ITEM_ERRORS: dict[int | str, dict[str, Any]] = {
    **_UNAUTHENTICATED,
    404: {"model": ErrorResponse, "description": "Unknown item"},
}


@router.get("/bookmarks", response_model=BookmarkPage, responses=_LIST_ERRORS)
def list_bookmarks(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    cursor: Annotated[str | None, Query(description="Opaque cursor from a previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=FEED_MAX_PAGE_SIZE)] = FEED_DEFAULT_PAGE_SIZE,
) -> BookmarkPage:
    """The current user's bookmarks, newest first."""
    try:
        return bookmarks_service.list_bookmarks(db, user, cursor=cursor, limit=limit)
    except InvalidCursorError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid cursor") from exc


@router.put("/items/{item_id}/bookmark", response_model=BookmarkOut, responses=_ITEM_ERRORS)
def put_bookmark(
    user: CurrentUser, db: Annotated[Session, Depends(get_db)], item_id: int
) -> BookmarkOut:
    """Create a bookmark idempotently; repeats return the existing one."""
    try:
        bookmark = bookmarks_service.create_bookmark(db, user, item_id)
    except ItemNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown item") from exc
    db.commit()
    return bookmark


@router.delete(
    "/items/{item_id}/bookmark", status_code=status.HTTP_204_NO_CONTENT, responses=_ITEM_ERRORS
)
def delete_bookmark(
    user: CurrentUser, db: Annotated[Session, Depends(get_db)], item_id: int
) -> Response:
    """Remove a bookmark; removing an absent bookmark is a 204 no-op."""
    try:
        bookmarks_service.remove_bookmark(db, user, item_id)
    except ItemNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown item") from exc
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
