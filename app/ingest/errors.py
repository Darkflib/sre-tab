"""Ingest exception hierarchy.

Every failure a source can produce is one of these, and every one carries
a stable ``error_class`` string so the per-source status surface and the
structured logs classify failures the same way.

The split that matters for acceptance criterion 5:

``UnsafeTargetError``
    The SSRF guard refused a URL. Raised *before* a socket is opened, on
    the entry URL and again on every redirect hop.
``SourceConfigurationError``
    The source is misconfigured — a non-feed endpoint, or a URL that is
    not the configured feed URL of an enabled source. Also pre-network.
"""

from __future__ import annotations


class IngestError(Exception):
    """Base class for every ingest failure."""

    @property
    def error_class(self) -> str:
        return type(self).__name__


class UnsafeTargetError(IngestError):
    """A fetch target was rejected by the SSRF guard.

    ``reason`` is a short stable token (``loopback``, ``private``,
    ``scheme``, ``port``, ``credentials``, ...) suitable for logging and
    for assertions in tests.
    """

    def __init__(self, url: str, reason: str, detail: str | None = None) -> None:
        self.url = url
        self.reason = reason
        self.detail = detail
        message = f"refused {reason}: {url}" + (f" ({detail})" if detail else "")
        super().__init__(message)


class SourceConfigurationError(IngestError):
    """The source cannot be fetched as configured.

    v1 parses RSS and Atom only; a source needing sitemap crawling,
    GraphQL, or any bespoke adapter is rejected here rather than growing
    a parser special case (PLAN, "Deferred to v2").
    """


class FetchError(IngestError):
    """Transport-level failure talking to a source."""


class ResponseTooLargeError(FetchError):
    """Body exceeded ``settings.source_fetch_max_bytes`` while streaming."""


class TooManyRedirectsError(FetchError):
    """Redirect chain exceeded ``settings.source_fetch_max_redirects``."""


class UpstreamStatusError(FetchError):
    """Source answered with a 4xx/5xx status."""

    def __init__(self, url: str, status_code: int) -> None:
        self.url = url
        self.status_code = status_code
        super().__init__(f"HTTP {status_code} from {url}")


class FetchTimeoutError(FetchError):
    """Whole-fetch deadline expired (redirect hops share one budget)."""


class ParseError(IngestError):
    """Body was not parseable as a supported feed."""


class UnsafeDocumentError(ParseError):
    """XML carried a DTD, entity declaration, or external reference."""


class UnsupportedFeedFormatError(ParseError):
    """Parsed, but not RSS or Atom."""


class DocumentTooComplexError(ParseError):
    """More elements or entries than ``MAX_ELEMENTS`` / ``MAX_ENTRY_ELEMENTS``.

    Distinct from ``ResponseTooLargeError``, which counts bytes on the
    wire. This one counts *nodes*, because that is what a DOM parser
    allocates against: a document can sit inside the byte cap and still
    expand by a factor of twenty on the way to a tree.
    """
