"""Feed route. All database access sits in ``app.services.feed``; this
module only translates HTTP to that call and service errors back."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.api.v1.schemas import ErrorResponse, FeedPage
from app.api.v1.schemas.feed import FEED_DEFAULT_PAGE_SIZE, FEED_MAX_PAGE_SIZE
from app.db.session import get_db
from app.services import feed as feed_service
from app.services.errors import InvalidCursorError

router = APIRouter(tags=["feed"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Malformed cursor"},
    401: {"model": ErrorResponse, "description": "Not signed in"},
}


@router.get("/feed", response_model=FeedPage, responses=_ERRORS)
def get_feed(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    topics: Annotated[
        list[str] | None, Query(description="Topic slugs to include; omit for the user's selection")
    ] = None,
    sources: Annotated[
        list[str] | None,
        Query(description="Source slugs to include; omit for the user's selection"),
    ] = None,
    cursor: Annotated[str | None, Query(description="Opaque cursor from a previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=FEED_MAX_PAGE_SIZE)] = FEED_DEFAULT_PAGE_SIZE,
) -> FeedPage:
    """Deduplicated feed ordered by publication time, newest first."""
    try:
        return feed_service.get_feed_page(
            db, user, topics=topics, sources=sources, cursor=cursor, limit=limit
        )
    except InvalidCursorError as exc:
        # A cursor is opaque, so a client cannot repair one; 400 tells it
        # to restart the listing. Letting the decode error escape would
        # be a 500 for what is a client-supplied value.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid cursor") from exc
