"""Guarded HTTP fetch for feed bodies.

Redirects are driven by hand. ``httpx`` will happily follow a 302 into
``169.254.169.254`` because its redirect handling sits below anything we
could hook, so ``follow_redirects`` is off and every hop goes back
through :class:`~app.ingest.urlguard.UrlGuard` as if it were a fresh
target.

Three properties this module is responsible for:

**Pinning.** The guard resolves the hostname and hands back one
validated address. The request is issued against that literal address
with ``Host`` and TLS SNI set to the original name, so the connection
lands on the address that was judged. No second, unvalidated resolution
happens anywhere in the path.

**Proxy independence.** The client is built with ``trust_env=False``.
An ``HTTPS_PROXY`` in the environment would otherwise route the
connection through a proxy of someone else's choosing, and the pinned
address would mean nothing.

**Size.** ``Content-Length`` is used only to fail early; the cap that
actually holds is counted over the streamed chunks.

**Time.** ``source_fetch_timeout_seconds`` is a deadline for the whole
fetch, not a per-operation timeout. ``httpx.Timeout`` bounds each
individual read, so a server dribbling one byte just inside it would
otherwise hold the connection for max-bytes ÷ dribble-rate — and the
scheduler ticks sources serially, so one such source stalls every
refresh. The deadline is therefore re-checked between redirect hops
*and* on every chunk of the body.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Collection, Iterator
from dataclasses import dataclass

import httpx
import structlog

from app.ingest.errors import (
    FetchError,
    FetchTimeoutError,
    ResponseTooLargeError,
    TooManyRedirectsError,
    UpstreamStatusError,
)
from app.ingest.urlguard import UrlGuard, ValidatedTarget
from app.settings import Settings

log = structlog.get_logger("app.ingest.fetch")

#: Minimum gap between requests to one host, redirect hops included.
#: Not a setting: settings.py is Phase 0 property and this is a politeness
#: floor rather than an operator dial.
DEFAULT_MIN_HOST_INTERVAL_SECONDS = 1.0

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Absent and `identity` are the same thing; anything else is a decompression
# bomb waiting to be counted after the fact. See `_read_capped`.
_ALLOWED_CODINGS = frozenset({"", "identity"})


class HostRateLimiter:
    """Serialise requests per host with a minimum interval between them."""

    def __init__(self, min_interval: float = DEFAULT_MIN_HOST_INTERVAL_SECONDS) -> None:
        self._min_interval = min_interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            earliest = self._last.get(host, 0.0) + self._min_interval
            delay = max(0.0, earliest - now)
            self._last[host] = now + delay
        if delay:
            time.sleep(delay)


@dataclass(frozen=True)
class FetchResult:
    url: str
    content: bytes
    content_type: str | None
    status_code: int
    #: Every URL validated on the way here, entry URL first.
    hops: tuple[str, ...]


class FeedFetcher:
    """Fetches one feed body, or raises. Never follows a hop unchecked."""

    def __init__(
        self,
        settings: Settings,
        *,
        guard: UrlGuard | None = None,
        rate_limiter: HostRateLimiter | None = None,
    ) -> None:
        self._settings = settings
        self._guard = guard or UrlGuard()
        self._rate_limiter = rate_limiter or HostRateLimiter()

    # -- public ----------------------------------------------------------

    def fetch(self, url: str, *, allowed_urls: Collection[str]) -> FetchResult:
        """Fetch *url*, which must be the feed URL of an enabled source.

        ``allowed_urls`` gates the entry URL only. A source is entitled
        to redirect, so hops are not required to be pre-declared — but
        every hop takes the identical scheme/credentials/port/host/DNS
        check, which is the part that matters.
        """
        deadline = time.monotonic() + self._settings.source_fetch_timeout_seconds
        max_redirects = self._settings.source_fetch_max_redirects
        current = url
        hops: list[str] = []

        with self._build_client() as client:
            for hop in range(max_redirects + 1):
                target = self._guard.validate(
                    current, allowed_urls=allowed_urls if hop == 0 else None
                )
                hops.append(str(target.url))
                self._rate_limiter.wait(target.host)

                outcome = self._request(client, target, deadline, tuple(hops))
                if isinstance(outcome, FetchResult):
                    return outcome

                current = str(target.url.join(outcome))
                log.debug("feed_fetch_redirect", hop=hop, from_url=str(target.url), to_url=current)

        raise TooManyRedirectsError(f"more than {max_redirects} redirects starting at {url}")

    # -- internals -------------------------------------------------------

    def _build_client(self) -> httpx.Client:
        return httpx.Client(
            follow_redirects=False,
            # Environment proxies would defeat the address pin.
            trust_env=False,
            verify=True,
            headers={
                "User-Agent": self._settings.source_fetch_user_agent,
                "Accept": "application/atom+xml, application/rss+xml, application/xml;q=0.9, "
                "text/xml;q=0.9, */*;q=0.1",
                # Not gzip: the size cap counts decompressed bytes, so a
                # coding lets a tiny body blow past it. `_read_capped`
                # refuses anything an origin sends regardless.
                "Accept-Encoding": "identity",
            },
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

    def _request(
        self,
        client: httpx.Client,
        target: ValidatedTarget,
        deadline: float,
        hops: tuple[str, ...],
    ) -> FetchResult | str:
        """One hop: the body if this is the end of the chain, otherwise
        the ``Location`` to validate and follow."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchTimeoutError(f"fetch deadline expired before {target.url}")

        try:
            with client.stream(
                "GET",
                target.connect_url,
                # The literal address is in the URL; the name goes here
                # and in SNI so certificate verification still applies.
                headers={"Host": target.host},
                extensions={"sni_hostname": target.host},
                timeout=httpx.Timeout(remaining),
            ) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError(f"redirect without Location from {target.url}")
                    return str(location)
                if response.status_code >= 400:
                    raise UpstreamStatusError(str(target.url), response.status_code)

                body = self._read_capped(response, url=str(target.url), deadline=deadline)
                return FetchResult(
                    url=str(target.url),
                    content=body,
                    content_type=response.headers.get("content-type"),
                    status_code=response.status_code,
                    hops=hops,
                )
        except httpx.TimeoutException as exc:
            raise FetchTimeoutError(f"timeout fetching {target.url}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"transport error fetching {target.url}: {exc}") from exc

    def _read_capped(self, response: httpx.Response, *, url: str, deadline: float) -> bytes:
        cap = self._settings.source_fetch_max_bytes

        # Refuse content-codings outright. The byte counter below sees what
        # httpx has *already decompressed*, so with a coding in play the cap
        # bounds the wrong quantity: a 285-byte body carrying stacked gzip
        # layers expands past a gigabyte inside the decoder before the loop
        # gets a chance to look. `Accept-Encoding: identity` asks for none,
        # and this refuses one sent anyway -- the ask is a courtesy, this is
        # the enforcement. Measured against the shipped catalogue: all seven
        # feeds honour identity, at a cost of ~554 KB per full refresh.
        #
        # This check must stay ahead of the first read. It works because the
        # caller streams: with `client.stream(...)` the headers land before
        # any body is decoded. A plain `client.get()` would decode eagerly
        # during construction and this would be inspecting a corpse.
        coding = response.headers.get("content-encoding", "").strip().lower()
        if coding not in _ALLOWED_CODINGS:
            raise ResponseTooLargeError(
                f"{url} used content-coding {coding!r}; only identity is accepted"
            )

        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > cap:
            # An early-out only. The count below is what enforces the cap.
            raise ResponseTooLargeError(f"{url} declared {declared} bytes, cap is {cap}")

        chunks: list[bytes] = []
        total = 0
        for chunk in self._iter(response):
            # Two independent bounds on one loop: bytes, and time. Without
            # the second, a dribbling server is limited only by the first.
            if deadline - time.monotonic() <= 0:
                raise FetchTimeoutError(f"fetch deadline expired while reading {url}")
            total += len(chunk)
            if total > cap:
                raise ResponseTooLargeError(f"{url} exceeded {cap} bytes while streaming")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _iter(response: httpx.Response) -> Iterator[bytes]:
        return response.iter_bytes()
