"""The hostile-URL table — acceptance criterion 5.

Every rejection here happens with no socket opened: the guard is given a
resolver that records its calls, and the fetch-level test in
``test_fetch.py`` asserts the same table produces zero HTTP requests
under respx.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence

import pytest

from app.ingest.errors import IngestError, SourceConfigurationError, UnsafeTargetError
from app.ingest.urlguard import (
    UrlGuard,
    ValidatedTarget,
    classify_address,
    default_resolver,
)

FEED = "https://feeds.example.com/rss"


class RecordingResolver:
    """Stand-in for ``getaddrinfo`` that records every lookup."""

    def __init__(self, answers: dict[str, Sequence[str]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> Sequence[str]:
        self.calls.append((host, port))
        return self.answers.get(host, ["93.184.216.34"])


# --- The hostile table --------------------------------------------------

# (raw url, expected reason token). ``None`` means the rejection is a
# SourceConfigurationError rather than an UnsafeTargetError.
HOSTILE_URLS: list[tuple[str, str | None]] = [
    # Names that can never be a public feed — refused before DNS runs,
    # so the resolver's opinion of "localhost" is irrelevant.
    ("https://localhost/rss", "host"),
    ("https://metadata/computeMetadata/v1/", "host"),
    ("https://intranet.local/rss", "host"),
    ("https://metadata.google.internal/rss", "host"),
    # IP literals.
    ("https://127.0.0.1/rss", "loopback"),
    ("https://127.1/rss", "loopback"),
    ("https://0.0.0.0/rss", "unspecified"),
    ("https://169.254.169.254/latest/meta-data/", "link-local"),
    ("https://[::1]/rss", "loopback"),
    # ipaddress judges an IPv4-mapped address by its embedded IPv4, so
    # the reason is the embedded one; ::ffff:0:0/96 is blocked wholesale
    # regardless, which is what catches the public-mapped case below.
    ("https://[::ffff:127.0.0.1]/rss", "loopback"),
    ("https://[::ffff:8.8.8.8]/rss", "blocked-range"),
    ("https://[fe80::1]/rss", "link-local"),
    ("https://[fc00::1]/rss", "private"),
    ("https://[2002:7f00:1::]/rss", "private"),
    ("https://10.0.0.5/rss", "private"),
    ("https://10.255.255.254/rss", "private"),
    ("https://192.168.1.1/rss", "private"),
    ("https://172.16.0.1/rss", "private"),
    ("https://100.64.0.1/rss", "non-global"),
    ("https://224.0.0.1/rss", "multicast"),
    ("https://255.255.255.255/rss", "private"),
    # Credentials, schemes, ports.
    ("https://user:pass@feeds.example.com/rss", "credentials"),
    ("https://user@feeds.example.com/rss", "credentials"),
    ("file:///etc/passwd", "scheme"),
    ("gopher://feeds.example.com/1", "scheme"),
    ("http://feeds.example.com/rss", "scheme"),
    ("ftp://feeds.example.com/rss", "scheme"),
    ("data:text/xml,<rss/>", "scheme"),
    ("https://feeds.example.com:8443/rss", "port"),
    ("https://feeds.example.com:22/rss", "port"),
    # Obfuscated IPv4. httpx itself refuses the octal dotted-quad as a
    # malformed URL; the rest are decoded by the guard.
    ("https://2130706433/rss", "loopback"),
    ("https://0177.0.0.1/rss", "url"),
    ("https://0x7f.0.0.1/rss", "loopback"),
    ("https://0xa9fea9fe/rss", "link-local"),
    ("https://017700000001/rss", "loopback"),
    # The same forms with a trailing dot. The dot is what carries them
    # past httpx's parser, and the guard strips it when normalising the
    # host — so the obfuscated literal only exists after normalisation.
    # Every one still lands on an IngestError with an honest reason.
    ("https://0x7f.0.0.1./rss", "loopback"),
    ("https://127.1./rss", "loopback"),
    ("https://0177.1./rss", "loopback"),
    ("https://0.0.0.0./rss", "unspecified"),
    ("https://0177.0.0.1./rss", "host"),
    ("https://010.010.010.010./rss", "host"),
    # Not RSS/Atom — a configuration error, deferred to v2.
    ("https://feeds.example.com/graphql", None),
    ("https://feeds.example.com/sitemap.xml", None),
]


@pytest.mark.parametrize(("raw_url", "reason"), HOSTILE_URLS, ids=[u for u, _ in HOSTILE_URLS])
def test_hostile_urls_are_refused_without_dns(raw_url: str, reason: str | None) -> None:
    resolver = RecordingResolver()
    guard = UrlGuard(resolver=resolver)

    with pytest.raises(IngestError) as excinfo:
        guard.validate(raw_url)

    if reason is None:
        assert isinstance(excinfo.value, SourceConfigurationError)
    else:
        assert isinstance(excinfo.value, UnsafeTargetError)
        assert excinfo.value.reason == reason

    # The whole table is refused before DNS, let alone before a socket.
    assert resolver.calls == []


def test_hostile_urls_never_reach_a_resolved_address() -> None:
    """No entry in the table survives validation, whatever DNS says."""
    resolver = RecordingResolver({"feeds.example.com": ["93.184.216.34"]})
    guard = UrlGuard(resolver=resolver)
    for raw_url, _ in HOSTILE_URLS:
        with pytest.raises(IngestError):
            guard.validate(raw_url)


def test_rejection_is_always_an_ingest_error() -> None:
    """The module's declared error type is the *only* way out.

    ``copy_with`` re-parses the normalised host, so a URL that httpx
    accepted in its original form could be refused in its normalised one
    — ``https://0177.0.0.1./rss`` did exactly that, raising
    ``httpx.InvalidURL`` straight through the guard and being recorded as
    error_class="InvalidURL" instead of a classified unsafe target.
    """
    guard = UrlGuard(resolver=RecordingResolver())
    for raw_url in (
        "https://0177.0.0.1./rss",
        "https://010.010.010.010./rss",
        "https://192.168.001.001./rss",
        "https://0177.0.0.1.:443/rss",
    ):
        with pytest.raises(IngestError) as excinfo:
            guard.validate(raw_url)
        assert isinstance(excinfo.value, UnsafeTargetError)
        assert excinfo.value.error_class == "UnsafeTargetError"


# --- Allow-list ---------------------------------------------------------


def test_entry_url_must_be_a_configured_source_url() -> None:
    resolver = RecordingResolver()
    guard = UrlGuard(resolver=resolver)
    with pytest.raises(SourceConfigurationError):
        guard.validate("https://evil.example.com/rss", allowed_urls={FEED})
    assert resolver.calls == []


def test_allow_listed_url_passes() -> None:
    guard = UrlGuard(resolver=RecordingResolver({"feeds.example.com": ["93.184.216.34"]}))
    target = guard.validate(FEED, allowed_urls={FEED})
    assert target.host == "feeds.example.com"
    assert str(target.ip) == "93.184.216.34"


def test_allow_list_comparison_is_exact() -> None:
    guard = UrlGuard(resolver=RecordingResolver())
    for near_miss in (
        "https://feeds.example.com/rss?x=1",
        "https://feeds.example.com/rss/",
        "https://feeds.example.com.evil.test/rss",
    ):
        with pytest.raises(SourceConfigurationError):
            guard.validate(near_miss, allowed_urls={FEED})


# --- DNS answers --------------------------------------------------------


def test_any_private_answer_rejects_the_whole_answer_set() -> None:
    """A split answer set — one public, one private — gains nothing."""
    guard = UrlGuard(
        resolver=RecordingResolver({"feeds.example.com": ["93.184.216.34", "127.0.0.1"]})
    )
    with pytest.raises(UnsafeTargetError) as excinfo:
        guard.validate(FEED, allowed_urls={FEED})
    assert excinfo.value.reason == "loopback"


def test_empty_answer_set_is_refused() -> None:
    guard = UrlGuard(resolver=RecordingResolver({"feeds.example.com": []}))
    with pytest.raises(UnsafeTargetError) as excinfo:
        guard.validate(FEED, allowed_urls={FEED})
    assert excinfo.value.reason == "unresolvable"


def test_garbage_answer_is_refused() -> None:
    guard = UrlGuard(resolver=RecordingResolver({"feeds.example.com": ["not-an-ip"]}))
    with pytest.raises(UnsafeTargetError) as excinfo:
        guard.validate(FEED, allowed_urls={FEED})
    assert excinfo.value.reason == "resolution"


def test_resolution_happens_exactly_once() -> None:
    resolver = RecordingResolver({"feeds.example.com": ["93.184.216.34"]})
    guard = UrlGuard(resolver=resolver)
    guard.validate(FEED, allowed_urls={FEED})
    assert resolver.calls == [("feeds.example.com", 443)]


# --- Pinning ------------------------------------------------------------


def test_connect_url_pins_the_validated_address() -> None:
    guard = UrlGuard(resolver=RecordingResolver({"feeds.example.com": ["93.184.216.34"]}))
    target = guard.validate(FEED, allowed_urls={FEED})
    assert str(target.connect_url) == "https://93.184.216.34/rss"
    # The name survives for the Host header and TLS SNI.
    assert target.host == "feeds.example.com"


def test_connect_url_brackets_ipv6() -> None:
    guard = UrlGuard(resolver=RecordingResolver({"feeds.example.com": ["2606:4700::1111"]}))
    target = guard.validate(FEED, allowed_urls={FEED})
    assert str(target.connect_url) == "https://[2606:4700::1111]/rss"


def test_validated_target_is_frozen() -> None:
    target = ValidatedTarget(
        url=__import__("httpx").URL(FEED),
        host="feeds.example.com",
        port=443,
        ip=ipaddress.ip_address("93.184.216.34"),
        resolved=(ipaddress.ip_address("93.184.216.34"),),
    )
    with pytest.raises(AttributeError):
        target.host = "other"  # type: ignore[misc]


# --- Static surface -----------------------------------------------------


def test_check_static_needs_no_resolver() -> None:
    resolver = RecordingResolver()
    guard = UrlGuard(resolver=resolver)
    url = guard.check_static("https://feeds.example.com:443/rss")
    assert str(url) == "https://feeds.example.com/rss"
    assert resolver.calls == []


@pytest.mark.parametrize(
    "raw_url",
    [
        "https://feeds.example.com/rss\n",
        "https://feeds.example.com/r ss",
        " https://feeds.example.com/rss",
        "https://" + "a" * 3000 + ".example.com/rss",
        "https:///rss",
        "not a url at all",
        "https://feeds..example.com/rss",
        "https://-feeds.example.com/rss",
    ],
)
def test_malformed_urls_are_refused(raw_url: str) -> None:
    guard = UrlGuard(resolver=RecordingResolver())
    with pytest.raises(UnsafeTargetError):
        guard.check_static(raw_url)


def test_idn_host_is_punycoded() -> None:
    """The validated name is the punycode one that goes on the wire."""
    guard = UrlGuard(resolver=RecordingResolver({"xn--bcher-kva.example.com": ["93.184.216.34"]}))
    target = guard.validate("https://bücher.example.com/rss")
    assert target.host == "xn--bcher-kva.example.com"


# --- Address classification --------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["8.8.8.8", "93.184.216.34", "1.1.1.1", "2606:4700:4700::1111", "2a00:1450:4009::200e"],
)
def test_public_addresses_are_allowed(address: str) -> None:
    assert classify_address(ipaddress.ip_address(address)) is None


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "127.0.0.1",
        "10.1.2.3",
        "172.20.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.0.1",
        "192.0.2.1",
        "198.18.0.1",
        "198.51.100.1",
        "203.0.113.1",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "fe80::1",
        "fc00::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "2002:7f00:1::",
        "64:ff9b::7f00:1",
        "2001:db8::1",
    ],
)
def test_reserved_addresses_are_refused(address: str) -> None:
    assert classify_address(ipaddress.ip_address(address)) is not None


# --- the catalogue must still be fetchable ------------------------------


@pytest.mark.parametrize(
    "feed_url",
    [
        "https://news.ycombinator.com/rss",
        "https://lobste.rs/rss",
        "https://dev.to/feed",
        "https://lwn.net/headlines/newrss",
        "https://feeds.arstechnica.com/arstechnica/index/",
        "https://feeds.bbci.co.uk/news/rss.xml",
        "https://www.theguardian.com/uk/rss",
        "https://medium.com/feed/tag/python",
    ],
)
def test_the_initial_catalogue_passes_the_static_checks(feed_url: str) -> None:
    """A guard rule that breaks the shipped catalogue is too strict."""
    guard = UrlGuard(resolver=RecordingResolver())
    assert str(guard.check_static(feed_url)) == feed_url


def test_default_resolver_resolves_a_known_literal() -> None:
    """The default resolver is exercised without depending on real DNS."""
    assert default_resolver("93.184.216.34", 443) == ["93.184.216.34"]


def test_default_resolver_reports_failure_as_unsafe() -> None:
    with pytest.raises(UnsafeTargetError) as excinfo:
        default_resolver("invalid.invalid.", 443)
    assert excinfo.value.reason == "unresolvable"
