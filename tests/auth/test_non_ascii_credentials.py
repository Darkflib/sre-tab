"""Credential material that is not ASCII must be refused, not fatal.

``hmac.compare_digest`` raises ``TypeError: comparing strings with
non-ASCII characters is not supported`` rather than returning ``False``,
and uvicorn decodes request headers as latin-1 — so any byte in
0x80-0xff on a cookie, header, or query parameter reached that call as a
non-ASCII ``str``. The CSRF middleware catches only ``HTTPException``, so
it escaped as an unauthenticated 500.

The requests here are made against the raw ASGI app rather than through
``TestClient``: httpx refuses to *send* a non-ASCII header, which is
exactly why the defect survived the HTTP-level suite.
"""

from __future__ import annotations

import asyncio
from collections.abc import MutableMapping
from typing import Any

import pytest
from fastapi import FastAPI

from app.api.v1.auth import callback_failure_limiter

CLIENT_IP = "203.0.113.9"


def asgi_request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    headers: list[tuple[bytes, bytes]],
    query_string: bytes = b"",
) -> int:
    """Drive one request straight at the ASGI app; return its status.

    An exception escaping the app propagates out of here, which is the
    failure this module is about.
    """
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "root_path": "",
        "headers": [(b"host", b"testserver"), *headers],
        "client": (CLIENT_IP, 51234),
        "server": ("testserver", 443),
        "app": app,
    }
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(dict(message))

    asyncio.run(app(scope, receive, send))
    return int(next(m["status"] for m in sent if m["type"] == "http.response.start"))


def test_non_ascii_csrf_material_is_refused_not_fatal(app: FastAPI) -> None:
    """No valid session is needed: the middleware fires on the mere
    presence of a cookie named ``session``."""
    status = asgi_request(
        app,
        "PATCH",
        "/api/v1/me/preferences",
        headers=[
            (b"content-type", b"application/json"),
            (b"cookie", "session=anything; csrftoken=abc\xe9.def".encode("latin-1")),
            (b"x-csrf-token", "abc\xe9.def".encode("latin-1")),
        ],
    )
    assert status == 403


def test_non_ascii_oauth_state_is_refused_not_fatal(app: FastAPI) -> None:
    status = asgi_request(
        app,
        "GET",
        "/api/v1/auth/github/callback",
        headers=[(b"cookie", "oauth_state=\xe9".encode("latin-1"))],
        query_string=b"code=x&state=%E9",
    )
    assert status == 403


def test_non_ascii_oauth_state_signature_is_refused_not_fatal(app: FastAPI) -> None:
    """A three-part token gets past the cookie comparison and into
    ``StateStore.consume``, which compares the signature slice — a second
    call site with the same defect."""
    status = asgi_request(
        app,
        "GET",
        "/api/v1/auth/github/callback",
        headers=[(b"cookie", "oauth_state=a.b.\xe9".encode("latin-1"))],
        query_string=b"code=x&state=a.b.%E9",
    )
    assert status == 403


@pytest.mark.parametrize(
    "query_string", [b"code=x&state=%E9", b"code=x&state=a.b.%E9"], ids=["state", "signature"]
)
def test_a_non_ascii_callback_counts_against_the_failure_budget(
    app: FastAPI, query_string: bytes
) -> None:
    """Crashing before the limiter ran made this branch free to grind."""
    headers = [(b"cookie", "oauth_state=a.b.\xe9".encode("latin-1"))]
    for _ in range(callback_failure_limiter.limit):
        assert (
            asgi_request(
                app,
                "GET",
                "/api/v1/auth/github/callback",
                headers=headers,
                query_string=query_string,
            )
            == 403
        )
    assert (
        asgi_request(
            app,
            "GET",
            "/api/v1/auth/github/callback",
            headers=headers,
            query_string=query_string,
        )
        == 429
    )
