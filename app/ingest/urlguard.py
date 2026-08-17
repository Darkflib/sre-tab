"""SSRF guard for outbound feed fetches — acceptance criterion 5.

Nothing in this module opens a socket. :meth:`UrlGuard.validate` runs
every check and returns a :class:`ValidatedTarget` describing *exactly*
which IP address the caller may connect to; the fetcher connects to that
address and no other. A caller that resolves again independently would
reopen the DNS-rebinding hole this module exists to close.

Checks, in order (cheapest and most decisive first):

1. **Allow-list.** The entry URL must be byte-identical to the
   ``feed_url`` of an enabled source. v1 has no user-supplied feed URLs
   (PRD non-goals), so this alone rejects the whole class. Redirect hops
   cannot be allow-listed — a source is entitled to redirect — so they
   skip step 1 and take every other check.
2. **Scheme** must be ``https``. ``http``, ``file``, ``gopher``,
   ``ftp``, ``data``, and everything else are refused.
3. **No credentials** in the URL (``user:pass@host``).
4. **Port** must be 443. Any explicit non-standard port is refused.
5. **Host** must be a syntactically plausible hostname or IP literal.
   Numeric IPv4 forms — decimal (``2130706433``), octal (``0177.0.0.1``),
   hex (``0x7f.0.0.1``), and short forms (``127.1``) — are decoded here
   rather than left for the resolver, so the check is deterministic
   across platforms.
6. **Endpoint kind.** GraphQL and sitemap endpoints are configuration
   errors, not parser special cases (PLAN, "Deferred to v2").
7. **Resolution.** The hostname is resolved once. *Every* address the
   resolver returns must be publicly routable — one bad answer in a
   multi-answer set rejects the whole target, so a split-answer attack
   gains nothing.
8. **Pinning.** The first validated address is returned as
   ``connect_url``; the original hostname is returned separately for the
   ``Host`` header and TLS SNI, so certificate verification is still
   against the real name.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass

import httpx

from app.ingest.errors import SourceConfigurationError, UnsafeTargetError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: ``(host, port) -> [address string, ...]``. Injectable so the hostile
#: URL table is testable without touching the network or DNS.
Resolver = Callable[[str, int], Sequence[str]]

ALLOWED_SCHEMES = frozenset({"https"})
ALLOWED_PORTS = frozenset({443})

#: Explicit blocks, belt and braces over the ``ipaddress`` predicates.
#: ``::ffff:0:0/96`` is blocked wholesale: a resolver has no business
#: returning an IPv4-mapped IPv6 address, so every one of them is
#: refused rather than unwrapped and re-judged.
BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.88.99.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::ffff:0:0/96"),
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    ipaddress.ip_network("100::/64"),
    ipaddress.ip_network("2001::/32"),
    ipaddress.ip_network("2001:db8::/32"),
    ipaddress.ip_network("2002::/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)

#: Endpoint shapes that are not RSS/Atom and must be caught at
#: configuration time rather than after a fetch.
_UNSUPPORTED_PATH_MARKERS = ("/graphql",)
_UNSUPPORTED_PATH_SUFFIXES = ("/sitemap.xml", "/sitemap_index.xml", ".graphql")

_MAX_URL_LENGTH = 2048
_MAX_HOST_LENGTH = 253
_HOST_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-._")

#: Names that never belong to a public feed. Single-label hosts are
#: refused outright (``localhost``, ``metadata``, intranet short names):
#: a real feed URL is always an FQDN, so this closes the whole class
#: before DNS rather than trusting the resolver's view of it.
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".arpa",
    ".onion",
    ".test",
    ".invalid",
    ".example",
)


@dataclass(frozen=True)
class ValidatedTarget:
    """A URL cleared for one connection to one address.

    ``connect_url`` has the literal validated IP in the host position.
    ``host`` is the original name, for the ``Host`` header and SNI.
    """

    url: httpx.URL
    host: str
    port: int
    ip: IPAddress
    resolved: tuple[IPAddress, ...]

    @property
    def connect_url(self) -> httpx.URL:
        literal = f"[{self.ip}]" if self.ip.version == 6 else str(self.ip)
        return self.url.copy_with(host=literal, port=self.port)


def default_resolver(host: str, port: int) -> Sequence[str]:
    """Resolve via ``getaddrinfo``, dropping any IPv6 scope suffix."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeTargetError(host, "unresolvable", str(exc)) from exc
    return [str(info[4][0]).partition("%")[0] for info in infos]


def classify_address(ip: IPAddress) -> str | None:
    """Return a rejection reason for *ip*, or ``None`` if it is routable.

    Ordered most-specific first so the reason logged is the interesting
    one (``loopback`` rather than the ``private`` that also matches).
    """
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_private:
        return "private"
    if ip.is_reserved:
        return "reserved"
    if not ip.is_global:
        return "non-global"
    for network in BLOCKED_NETWORKS:
        if ip.version == network.version and ip in network:
            return "blocked-range"
    return None


def _embedded_addresses(ip: IPAddress) -> Iterable[IPAddress]:
    """IPv4 addresses tunnelled inside an IPv6 address.

    ``::ffff:127.0.0.1``, ``2002:7f00:1::`` (6to4), and Teredo all carry
    an IPv4 address that the outer predicates alone would not judge.
    """
    if not isinstance(ip, ipaddress.IPv6Address):
        return ()
    embedded: list[IPAddress] = []
    if ip.ipv4_mapped is not None:
        embedded.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        embedded.append(ip.sixtofour)
    if ip.teredo is not None:
        embedded.extend(ip.teredo)
    return embedded


def assert_address_allowed(ip: IPAddress, *, url: str) -> None:
    """Raise :class:`UnsafeTargetError` unless *ip* is publicly routable."""
    reason = classify_address(ip)
    if reason is not None:
        raise UnsafeTargetError(url, reason, f"resolved to {ip}")
    for embedded in _embedded_addresses(ip):
        inner = classify_address(embedded)
        if inner is not None:
            raise UnsafeTargetError(url, inner, f"{ip} embeds {embedded}")


def _parse_numeric_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Decode ``inet_aton``-style IPv4 forms the resolver would accept.

    Handles decimal, octal (leading ``0``), and hex (leading ``0x``)
    parts, and the 1-, 2-, and 3-part short forms. Returns ``None`` for
    anything that is not one of those, including ordinary hostnames.
    """
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    values: list[int] = []
    for part in parts:
        if not part:
            return None
        try:
            if part.lower().startswith("0x"):
                value = int(part, 16)
            elif part.startswith("0") and len(part) > 1:
                value = int(part, 8)
            elif part.isdigit():
                value = int(part, 10)
            else:
                return None
        except ValueError:
            return None
        values.append(value)

    # inet_aton: the final part absorbs the remaining low-order bytes.
    leading, last = values[:-1], values[-1]
    remaining = 4 - len(leading)
    if any(value > 0xFF for value in leading) or last >= 1 << (8 * remaining):
        return None
    packed = 0
    for value in leading:
        packed = (packed << 8) | value
    packed = (packed << (8 * remaining)) | last
    try:
        return ipaddress.IPv4Address(packed)
    except ipaddress.AddressValueError:
        return None


def ascii_host(url: httpx.URL) -> str:
    """The host httpx will actually put on the wire.

    ``URL.host`` returns the *decoded* IDN form; ``raw_host`` is the
    punycode. The guard works in punycode throughout so the name it
    validates is the name sent as ``Host`` and TLS SNI.
    """
    return url.raw_host.decode("ascii").lower()


def _normalise_host(raw: str, *, url: str) -> str:
    host = raw.strip().rstrip(".").lower()
    if not host:
        raise UnsafeTargetError(url, "host", "empty host")
    if len(host) > _MAX_HOST_LENGTH:
        raise UnsafeTargetError(url, "host", "host too long")
    if not host.isascii():
        raise UnsafeTargetError(url, "host", "non-ascii host")
    if set(host) - _HOST_ALLOWED:
        raise UnsafeTargetError(url, "host", "illegal character in host")
    if ".." in host or host.startswith((".", "-")) or host.endswith("-"):
        raise UnsafeTargetError(url, "host", "malformed host")
    if "." not in host:
        raise UnsafeTargetError(url, "host", "single-label host")
    if host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise UnsafeTargetError(url, "host", "non-public name")
    return host


def assert_supported_endpoint(url: httpx.URL) -> None:
    """Reject endpoints v1's RSS/Atom-only fetcher cannot serve."""
    path = url.path.lower()
    if any(marker in path for marker in _UNSUPPORTED_PATH_MARKERS) or path.endswith(
        _UNSUPPORTED_PATH_SUFFIXES
    ):
        raise SourceConfigurationError(
            f"{url} is not an RSS/Atom endpoint; bespoke adapters are deferred to v2"
        )


class UrlGuard:
    """Validates fetch targets. Holds no state beyond its resolver."""

    def __init__(self, resolver: Resolver | None = None) -> None:
        self._resolve = resolver if resolver is not None else default_resolver

    def check_static(self, raw_url: str) -> httpx.URL:
        """Every check that needs no DNS. Safe to call at config time."""
        if len(raw_url) > _MAX_URL_LENGTH:
            raise UnsafeTargetError(raw_url[:120], "url", "url too long")
        if raw_url.strip() != raw_url or any(ch in raw_url for ch in "\r\n\t "):
            raise UnsafeTargetError(raw_url[:120], "url", "whitespace in url")
        try:
            url = httpx.URL(raw_url)
        except (httpx.InvalidURL, ValueError) as exc:
            raise UnsafeTargetError(raw_url[:120], "url", str(exc)) from exc

        if url.scheme.lower() not in ALLOWED_SCHEMES:
            raise UnsafeTargetError(raw_url, "scheme", f"scheme {url.scheme!r} is not https")
        if url.userinfo:
            raise UnsafeTargetError(raw_url, "credentials", "credentials in url")
        if url.port is not None and url.port not in ALLOWED_PORTS:
            raise UnsafeTargetError(raw_url, "port", f"port {url.port} is not permitted")

        raw_host = ascii_host(url)
        # An IP literal — including an obfuscated one — needs no DNS, so
        # it is judged here and reported with its real reason rather than
        # falling through to a vague hostname-syntax rejection.
        literal = _literal_address(raw_host)
        if literal is not None:
            assert_address_allowed(literal, url=raw_url)
            host = str(literal)
        else:
            host = _normalise_host(raw_host, url=raw_url)

        try:
            # copy_with re-parses, so a host that survived httpx's parser
            # in its original form can be refused in its normalised one —
            # `https://0177.0.0.1./rss` does exactly that, the trailing
            # dot getting it past the first parse. Still a rejection, but
            # httpx.InvalidURL is not an IngestError and was recorded as
            # error_class="InvalidURL" rather than a classified target.
            normalised = url.copy_with(host=host, port=None)
        except (httpx.InvalidURL, ValueError) as exc:
            raise UnsafeTargetError(raw_url[:120], "host", str(exc)) from exc
        assert_supported_endpoint(normalised)
        return normalised

    def validate(
        self, raw_url: str, *, allowed_urls: Collection[str] | None = None
    ) -> ValidatedTarget:
        """Full check. ``allowed_urls`` is supplied for the entry URL and
        omitted for redirect hops, which cannot be pre-declared."""
        if allowed_urls is not None and raw_url not in allowed_urls:
            raise SourceConfigurationError(
                "refused target that is not the feed URL of an enabled source"
            )

        url = self.check_static(raw_url)
        host = ascii_host(url)
        port = url.port or 443

        literal = _literal_address(host)
        if literal is not None:
            # Re-asserted rather than assumed: check_static already
            # cleared it, and the cost of proving it twice is nil.
            assert_address_allowed(literal, url=raw_url)
            return ValidatedTarget(url=url, host=host, port=port, ip=literal, resolved=(literal,))

        answers = self._resolve(host, port)
        if not answers:
            raise UnsafeTargetError(raw_url, "unresolvable", "no addresses returned")

        resolved: list[IPAddress] = []
        for answer in answers:
            try:
                ip = ipaddress.ip_address(answer)
            except ValueError as exc:
                raise UnsafeTargetError(raw_url, "resolution", f"bad answer {answer!r}") from exc
            # Every answer must pass: a split answer set gains nothing.
            assert_address_allowed(ip, url=raw_url)
            resolved.append(ip)

        return ValidatedTarget(
            url=url, host=host, port=port, ip=resolved[0], resolved=tuple(resolved)
        )


def _literal_address(host: str) -> IPAddress | None:
    """The host as an IP address, including obfuscated IPv4 forms."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if host.startswith("[") and host.endswith("]"):
        try:
            return ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            return None
    return _parse_numeric_ipv4(host)
