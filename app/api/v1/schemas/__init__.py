"""Frozen request/response contract for /api/v1 (Phase 0 property).

Phase 1 agents import from these modules and never edit them; a schema
gap is escalated. The auth endpoints have no module here deliberately —
they carry no JSON bodies (redirects and a 204), so there is nothing to
freeze.

Topics and sources are referenced by slug throughout the API surface;
numeric IDs stay internal to the database.
"""

from app.api.v1.schemas.bookmarks import BookmarkOut, BookmarkPage
from app.api.v1.schemas.common import ErrorResponse
from app.api.v1.schemas.feed import FeedItemOut, FeedPage, FeedSourceRef
from app.api.v1.schemas.health import HealthResponse, ProbeStatus
from app.api.v1.schemas.items import ReadStateOut, ReadStateUpdate
from app.api.v1.schemas.me import MeResponse, PreferencesOut, PreferencesPatch, UserOut
from app.api.v1.schemas.sources import SourceOut, SourcesResponse, TopicOut

__all__ = [
    "BookmarkOut",
    "BookmarkPage",
    "ErrorResponse",
    "FeedItemOut",
    "FeedPage",
    "FeedSourceRef",
    "HealthResponse",
    "MeResponse",
    "PreferencesOut",
    "PreferencesPatch",
    "ProbeStatus",
    "ReadStateOut",
    "ReadStateUpdate",
    "SourceOut",
    "SourcesResponse",
    "TopicOut",
    "UserOut",
]
