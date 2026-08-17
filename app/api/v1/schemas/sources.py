from __future__ import annotations

from pydantic import Field

from app.api.v1.schemas.common import ApiModel


class TopicOut(ApiModel):
    slug: str
    name: str
    enabled: bool


class SourceOut(ApiModel):
    slug: str
    name: str
    feed_url: str
    website_url: str
    icon_url: str | None
    refresh_minutes: int
    enabled: bool
    topics: list[str] = Field(description="Default topic slugs for this source")


class SourcesResponse(ApiModel):
    sources: list[SourceOut]
    topics: list[TopicOut]
