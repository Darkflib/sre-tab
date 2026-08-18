"""Security-headers middleware (PRD, Deployment)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.datastructures import MutableHeaders

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Restrictive default: same-origin everything, no framing, no objects.
# img-src additionally allows https: because feed item images and GitHub
# avatars are served from external hosts.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' https: data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

# Swagger UI loads its bundle from jsdelivr; the interactive docs get a
# CSP relaxed just enough for that, and only on the docs paths.
_DOCS_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' https: data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)

_STATIC_HEADERS = {
    # One year, and deliberately without `includeSubDomains` or `preload`.
    #
    # Caddy serves this stack over plain HTTP on 127.0.0.1:8080 and the
    # operator's proxy terminates TLS, so the header only ever reaches a
    # browser over HTTPS — which is the condition for it being honoured, and
    # the reason deploy/README.md now lists it among the headers that proxy
    # must not strip.
    #
    # `includeSubDomains` is the right setting for the documented topology, a
    # dedicated host like news.example.com, and is the wrong thing to *default*
    # to: on an apex deployment it forces HTTPS across every unrelated
    # subdomain the operator owns, for a year, with no way to take it back
    # early. That is an outage a self-hoster inherits from a default they never
    # chose. deploy/README.md says how to add it and when it is safe.
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
}


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        csp = _DOCS_CSP if path == "/docs" else _CSP

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", csp)
                for name, value in _STATIC_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_wrapper)
