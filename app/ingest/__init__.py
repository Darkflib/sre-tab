"""Feed ingest: SSRF-guarded fetch, RSS/Atom parse, normalise, store.

Public surface, in the order a refresh uses it:

``urlguard``   validate a target before any socket is opened
``fetch``      stream a bounded body, re-validating every redirect hop
``parse``      defused XML, RSS and Atom only
``normalise``  plain-text summaries, UTC timestamps, canonical URLs
``store``      idempotent upsert on ``feed_items.canonical_url``
``status``     per-source outcome for the operator surface
``service``    the per-source orchestration, isolated from its neighbours
"""

from __future__ import annotations

from app.ingest.errors import (
    DocumentTooComplexError,
    FetchError,
    FetchTimeoutError,
    IngestError,
    ParseError,
    ResponseTooLargeError,
    SourceConfigurationError,
    TooManyRedirectsError,
    UnsafeDocumentError,
    UnsafeTargetError,
    UnsupportedFeedFormatError,
    UpstreamStatusError,
)

__all__ = [
    "DocumentTooComplexError",
    "FetchError",
    "FetchTimeoutError",
    "IngestError",
    "ParseError",
    "ResponseTooLargeError",
    "SourceConfigurationError",
    "TooManyRedirectsError",
    "UnsafeDocumentError",
    "UnsafeTargetError",
    "UnsupportedFeedFormatError",
    "UpstreamStatusError",
]
