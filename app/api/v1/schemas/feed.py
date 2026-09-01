from __future__ import annotations

import enum
from datetime import datetime

from pydantic import Field

from app.api.v1.schemas.common import ApiModel

FEED_DEFAULT_PAGE_SIZE = 25
FEED_MAX_PAGE_SIZE = 100


# How GET /feed narrows on the caller's read state.
#
# A named enum rather than a nullable boolean, and the choice is about the
# third state. The dimension has three values — every item, unread only,
# read only — and a nullable `read` spells the first as *absent* and the
# second as *false*, which puts the distinction that matters into the
# presence of a parameter rather than into its value. In a URL a user can
# edit and share that is invisible: `?read=false` reads as "not filtering
# on read" at least as readily as "unread only", and deleting it changes
# the result. `?read_state=unread` says which of the three it is.
#
# It also survives the round trip better: pydantic emits this as a named
# component, so openapi-typescript gives the client a union type it can
# import rather than an anonymous `boolean | null` whose three states it
# would have to re-document at every use.
#
# ALL is the default and is what an absent parameter means, so an existing
# client that sends nothing sees no change. It is spelled explicitly rather
# than left as None so the vocabulary is total: a hand-edited URL saying
# `all` gets the feed rather than a 422.
#
# Comments rather than a docstring deliberately — a class docstring becomes
# the component's `description` in the published contract, and this is
# reasoning for the server, not documentation for a client.
class ReadFilter(enum.StrEnum):
    ALL = "all"
    UNREAD = "unread"
    READ = "read"


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
