"""Fetch behaviour, with respx as the witness that no socket was opened.

respx intercepts at the transport, below everything the fetcher does, so
``respx.calls`` is a truthful record of what was actually attempted.
"""

from __future__ import annotations

import gzip
import time
import zlib
from collections.abc import Iterator

import httpx
import pytest
import respx

from app.ingest.errors import (
    FetchError,
    FetchTimeoutError,
    IngestError,
    ResponseTooLargeError,
    SourceConfigurationError,
    TooManyRedirectsError,
    UnsafeTargetError,
    UpstreamStatusError,
)
from app.ingest.fetch import FeedFetcher, HostRateLimiter
from app.ingest.urlguard import UrlGuard
from app.settings import Settings
from tests.ingest.conftest import (
    FEED_URL,
    OTHER_IP,
    PINNED_URL,
    TEST_HOST,
    TEST_IP,
    StubResolver,
)
from tests.ingest.test_urlguard import HOSTILE_URLS

BODY = b"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title></channel></rss>"


# --- criterion 5 at the fetch layer -------------------------------------


@respx.mock
def test_hostile_urls_produce_no_http_request(fetcher: FeedFetcher) -> None:
    """The whole hostile table, refused with zero requests attempted."""
    catch_all = respx.route().mock(return_value=httpx.Response(200, content=BODY))
    for raw_url, _ in HOSTILE_URLS:
        with pytest.raises(IngestError):
            fetcher.fetch(raw_url, allowed_urls={raw_url})
    assert catch_all.call_count == 0
    assert len(respx.calls) == 0


@respx.mock
def test_url_outside_the_allow_list_produces_no_request(fetcher: FeedFetcher) -> None:
    respx.route().mock(return_value=httpx.Response(200, content=BODY))
    with pytest.raises(SourceConfigurationError):
        fetcher.fetch("https://cdn.example.net/rss", allowed_urls={FEED_URL})
    assert len(respx.calls) == 0


# --- the happy path and its pinning -------------------------------------


@respx.mock
def test_fetch_connects_to_the_validated_address(fetcher: FeedFetcher) -> None:
    route = respx.get(PINNED_URL).mock(
        return_value=httpx.Response(200, content=BODY, headers={"content-type": "text/xml"})
    )
    result = fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})

    assert result.content == BODY
    assert result.url == FEED_URL
    assert result.hops == (FEED_URL,)

    request = route.calls.last.request
    # Connection is to the literal address the guard cleared...
    assert request.url.host == TEST_IP
    # ...while the name survives for Host and TLS SNI, so certificate
    # verification is still against the real hostname.
    assert request.headers["host"] == TEST_HOST
    assert request.extensions["sni_hostname"] == TEST_HOST


@respx.mock
def test_fetch_resolves_exactly_once(fetcher: FeedFetcher, resolver: StubResolver) -> None:
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=BODY))
    fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})
    # One lookup, and the fetcher never asks the OS resolver again: the
    # address in the request URL is the one that was judged.
    assert resolver.calls == [(TEST_HOST, 443)]


# --- redirects ----------------------------------------------------------


@respx.mock
def test_every_redirect_hop_is_revalidated(fetcher: FeedFetcher) -> None:
    respx.get(PINNED_URL).mock(
        return_value=httpx.Response(302, headers={"location": "https://cdn.example.net/rss"})
    )
    final = respx.get(f"https://{OTHER_IP}/rss").mock(
        return_value=httpx.Response(200, content=BODY)
    )

    result = fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})

    assert result.url == "https://cdn.example.net/rss"
    assert result.hops == (FEED_URL, "https://cdn.example.net/rss")
    # The second hop was pinned in its own right, not merely followed.
    assert final.calls.last.request.url.host == OTHER_IP
    assert final.calls.last.request.headers["host"] == "cdn.example.net"


@respx.mock
def test_redirect_into_a_private_address_is_refused(fetcher: FeedFetcher) -> None:
    respx.get(PINNED_URL).mock(
        return_value=httpx.Response(302, headers={"location": "https://private.example.com/rss"})
    )
    trap = respx.get("https://10.0.0.7/rss").mock(return_value=httpx.Response(200, content=BODY))

    with pytest.raises(UnsafeTargetError) as excinfo:
        fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})

    assert excinfo.value.reason == "private"
    assert trap.call_count == 0


@respx.mock
def test_redirect_into_link_local_metadata_is_refused(fetcher: FeedFetcher) -> None:
    respx.get(PINNED_URL).mock(
        return_value=httpx.Response(
            302, headers={"location": "https://metadata-host.example.com/latest/meta-data/"}
        )
    )
    trap = respx.get("https://169.254.169.254/latest/meta-data/").mock(
        return_value=httpx.Response(200, content=b"secrets")
    )

    with pytest.raises(UnsafeTargetError) as excinfo:
        fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})

    assert excinfo.value.reason == "link-local"
    assert trap.call_count == 0


@respx.mock
def test_redirect_downgrade_to_http_is_refused(fetcher: FeedFetcher) -> None:
    respx.get(PINNED_URL).mock(
        return_value=httpx.Response(301, headers={"location": "http://feeds.example.com/rss"})
    )
    with pytest.raises(UnsafeTargetError) as excinfo:
        fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})
    assert excinfo.value.reason == "scheme"


@respx.mock
def test_redirect_to_a_file_url_is_refused(fetcher: FeedFetcher) -> None:
    respx.get(PINNED_URL).mock(
        return_value=httpx.Response(302, headers={"location": "file:///etc/passwd"})
    )
    with pytest.raises(UnsafeTargetError) as excinfo:
        fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})
    assert excinfo.value.reason == "scheme"


@respx.mock
def test_relative_redirect_resolves_against_the_hostname(fetcher: FeedFetcher) -> None:
    respx.get(PINNED_URL).mock(return_value=httpx.Response(302, headers={"location": "/feed.xml"}))
    second = respx.get(f"https://{TEST_IP}/feed.xml").mock(
        return_value=httpx.Response(200, content=BODY)
    )
    result = fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})
    assert result.url == "https://feeds.example.com/feed.xml"
    assert second.calls.last.request.headers["host"] == TEST_HOST


@respx.mock
def test_redirect_loop_is_bounded(fetcher: FeedFetcher, ingest_settings: Settings) -> None:
    respx.get(PINNED_URL).mock(return_value=httpx.Response(302, headers={"location": FEED_URL}))
    with pytest.raises(TooManyRedirectsError):
        fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})
    assert len(respx.calls) == ingest_settings.source_fetch_max_redirects + 1


@respx.mock
def test_redirect_without_location_is_an_error(fetcher: FeedFetcher) -> None:
    respx.get(PINNED_URL).mock(return_value=httpx.Response(302))
    with pytest.raises(FetchError):
        fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})


# --- size cap -----------------------------------------------------------


@respx.mock
def test_oversized_body_is_refused_while_streaming(
    fetcher: FeedFetcher, ingest_settings: Settings
) -> None:
    oversized = b"x" * (ingest_settings.source_fetch_max_bytes + 1)
    # No Content-Length: the streamed count is what enforces the cap.
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, stream=httpx.ByteStream(oversized)))
    with pytest.raises(ResponseTooLargeError):
        fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})


@respx.mock
def test_lying_content_length_does_not_lift_the_cap(
    fetcher: FeedFetcher, ingest_settings: Settings
) -> None:
    """A small declared length over a large body is still refused."""
    oversized = b"x" * (ingest_settings.source_fetch_max_bytes + 4096)
    respx.get(PINNED_URL).mock(
        return_value=httpx.Response(
            200, headers={"content-length": "10"}, stream=httpx.ByteStream(oversized)
        )
    )
    with pytest.raises(ResponseTooLargeError):
        fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})


@respx.mock
def test_declared_oversize_fails_before_reading(
    fetcher: FeedFetcher, ingest_settings: Settings
) -> None:
    cap = ingest_settings.source_fetch_max_bytes
    respx.get(PINNED_URL).mock(
        return_value=httpx.Response(200, headers={"content-length": str(cap * 10)}, content=b"x")
    )
    with pytest.raises(ResponseTooLargeError):
        fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})


@respx.mock
def test_body_at_the_cap_is_accepted(fetcher: FeedFetcher, ingest_settings: Settings) -> None:
    exact = b"x" * ingest_settings.source_fetch_max_bytes
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=exact))
    assert fetcher.fetch(FEED_URL, allowed_urls={FEED_URL}).content == exact


# --- transport failures -------------------------------------------------


@respx.mock
def test_upstream_error_status_is_classified(fetcher: FeedFetcher) -> None:
    respx.get(PINNED_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(UpstreamStatusError) as excinfo:
        fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})
    assert excinfo.value.status_code == 503


@respx.mock
def test_transport_error_is_wrapped(fetcher: FeedFetcher) -> None:
    respx.get(PINNED_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(FetchError):
        fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})


@respx.mock
def test_timeout_is_classified(fetcher: FeedFetcher) -> None:
    respx.get(PINNED_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FetchTimeoutError):
        fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})


# --- the deadline bounds the body, not just the handshake ---------------


class DribbleStream(httpx.SyncByteStream):
    """A body that arrives one byte at a time, never late enough for a
    single read to time out. ``httpx.Timeout`` is per-operation, so only
    a deadline checked inside the read loop stops this."""

    def __init__(self, chunks: int, gap: float) -> None:
        self.chunks = chunks
        self.gap = gap
        self.yielded = 0

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(self.chunks):
            time.sleep(self.gap)
            self.yielded += 1
            yield b"x"

    def close(self) -> None:
        pass


@respx.mock
def test_dribbled_body_cannot_outlive_the_deadline(
    ingest_settings: Settings, guard: UrlGuard
) -> None:
    """A slow-drip body is bounded by the deadline, not by the size cap.

    Before the fix this returned only once the body reached
    ``source_fetch_max_bytes`` — 64,000 dribbles here — which is how one
    hostile source stalls the serial scheduler tick for every other one.
    """
    settings = ingest_settings.model_copy(update={"source_fetch_timeout_seconds": 0.3})
    fetcher = FeedFetcher(settings, guard=guard, rate_limiter=HostRateLimiter(0.0))
    stream = DribbleStream(chunks=settings.source_fetch_max_bytes, gap=0.02)
    respx.get(PINNED_URL).mock(return_value=httpx.Response(200, stream=stream))

    started = time.monotonic()
    with pytest.raises(FetchTimeoutError):
        fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"the 0.3s deadline held for {elapsed:.1f}s"
    # Stopped early: nowhere near the size cap, which is the other bound.
    assert stream.yielded < settings.source_fetch_max_bytes


@respx.mock
def test_a_body_inside_the_deadline_is_not_disturbed(
    ingest_settings: Settings, guard: UrlGuard
) -> None:
    """The in-loop check must not clip a slow-but-punctual source."""
    settings = ingest_settings.model_copy(update={"source_fetch_timeout_seconds": 5.0})
    fetcher = FeedFetcher(settings, guard=guard, rate_limiter=HostRateLimiter(0.0))
    respx.get(PINNED_URL).mock(
        return_value=httpx.Response(200, stream=DribbleStream(chunks=len(BODY), gap=0.001))
    )
    assert fetcher.fetch(FEED_URL, allowed_urls={FEED_URL}).content == b"x" * len(BODY)


# --- client construction ------------------------------------------------


def test_client_ignores_environment_proxies(ingest_settings: Settings) -> None:
    """An HTTPS_PROXY would route around the pinned address."""
    fetcher = FeedFetcher(ingest_settings, rate_limiter=HostRateLimiter(0.0))
    with fetcher._build_client() as client:
        assert client.trust_env is False


def test_rate_limiter_spaces_requests_to_one_host() -> None:
    import time

    limiter = HostRateLimiter(0.05)
    started = time.monotonic()
    limiter.wait("a.example")
    limiter.wait("a.example")
    assert time.monotonic() - started >= 0.05


def test_rate_limiter_does_not_couple_hosts() -> None:
    import time

    limiter = HostRateLimiter(0.5)
    started = time.monotonic()
    limiter.wait("a.example")
    limiter.wait("b.example")
    assert time.monotonic() - started < 0.4


# --- H1: the size cap counts decompressed bytes ---------------------------


@respx.mock
def test_identity_is_requested_so_the_cap_counts_wire_bytes(fetcher: FeedFetcher) -> None:
    """The cap counts what httpx hands back, which is post-decompression.
    Asking for ``identity`` is what keeps that equal to the wire size."""
    route = respx.get(PINNED_URL).mock(return_value=httpx.Response(200, content=BODY))
    fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})
    assert route.calls.last.request.headers["accept-encoding"] == "identity"


def _gzip(payload: bytes, layers: int = 1) -> bytes:
    for _ in range(layers):
        payload = gzip.compress(payload)
    return payload


@respx.mock
@pytest.mark.parametrize(
    ("coding", "body"),
    [
        ("gzip", _gzip(b"\0" * (8 << 20))),
        ("GZIP", _gzip(b"\0" * (8 << 20))),
        ("deflate", zlib.compress(b"\0" * (8 << 20))),
        # The one that matters: httpx builds a decoder per comma-separated
        # value, so each layer multiplies for free. Tiny on the wire.
        ("gzip, gzip, gzip, gzip, gzip, gzip, gzip", _gzip(b"\0" * (8 << 20), layers=7)),
    ],
    ids=["gzip", "uppercase", "deflate", "stacked-x7"],
)
def test_a_content_coding_is_refused_before_it_is_decoded(
    fetcher: FeedFetcher, coding: str, body: bytes
) -> None:
    """A decompression bomb: trivial on the wire, enormous decoded. Refusing
    on the header is what stops the decoder running at all -- counting bytes
    afterwards counts them too late, because the allocation already happened."""
    respx.get(PINNED_URL).mock(
        return_value=httpx.Response(200, headers={"Content-Encoding": coding}, content=body)
    )
    with pytest.raises(ResponseTooLargeError, match="content-coding"):
        fetcher.fetch(FEED_URL, allowed_urls={FEED_URL})


@respx.mock
def test_an_explicit_identity_header_is_accepted(fetcher: FeedFetcher) -> None:
    respx.get(PINNED_URL).mock(
        return_value=httpx.Response(200, headers={"Content-Encoding": "identity"}, content=BODY)
    )
    assert fetcher.fetch(FEED_URL, allowed_urls={FEED_URL}).content == BODY
