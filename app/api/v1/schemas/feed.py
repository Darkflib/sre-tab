from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.api.v1.schemas.common import ApiModel

FEED_DEFAULT_PAGE_SIZE = 25
FEED_MAX_PAGE_SIZE = 100


class FeedSourceRef(ApiModel):
    slug: str
    name: str
    icon_url: str | None


class FeedItemOut(ApiModel):
    id: int
    canonical_url: str
    title: str
    summary: str | None
    image_url: str | None
    published_at: datetime
    source: FeedSourceRef
    topics: list[str] = Field(description="Topic slugs")
    # Per-user state folded in so the client renders cards in one call.
    read: bool
    bookmarked: bool


class FeedPage(ApiModel):
    items: list[FeedItemOut]
    next_cursor: str | None = Field(
        description="Opaque cursor for the next page; null when exhausted"
    )
