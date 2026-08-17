from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.api.v1.schemas.common import ApiModel
from app.api.v1.schemas.feed import FeedItemOut


class BookmarkOut(ApiModel):
    item: FeedItemOut
    created_at: datetime


class BookmarkPage(ApiModel):
    bookmarks: list[BookmarkOut]
    next_cursor: str | None = Field(
        description="Opaque cursor for the next page; null when exhausted"
    )
