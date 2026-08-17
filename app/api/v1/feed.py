"""Feed route — Phase 1 agent C replaces the body; contract fixed."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUser
from app.api.v1.schemas import ErrorResponse, FeedPage
from app.api.v1.schemas.feed import FEED_DEFAULT_PAGE_SIZE, FEED_MAX_PAGE_SIZE

router = APIRouter(tags=["feed"])

_NOT_IMPLEMENTED = HTTPException(
    status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented — Phase 1 (agent C)"
)


@router.get(
    "/feed",
    response_model=FeedPage,
    responses={401: {"model": ErrorResponse, "description": "Not signed in"}},
)
def get_feed(
    user: CurrentUser,
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
    raise _NOT_IMPLEMENTED
