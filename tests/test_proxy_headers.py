"""The client-address chain the rate limiter depends on.

``app.api.v1.auth._client_ip`` reads ``request.client.host`` and trusts no
header, which is right — but it only ever sees a real client address
because uvicorn rewrote ``scope["client"]`` first, and only for peers in
``FORWARDED_ALLOW_IPS``.

That makes uvicorn's behaviour part of this application's security
properties rather than an implementation detail of the server, so the
assumptions are pinned here. A uvicorn upgrade that changes the default,
or the direction it walks the forwarded chain, should fail in CI rather
than turn per-IP rate limiting into one global bucket in production —
which is a change with no symptom until somebody looks.

The middleware is exercised directly. Booting a socket server would test
the same code more slowly, and the deployment's other half — Caddy
appending rather than replacing ``X-Forwarded-For`` — cannot be reached
from Python at all. That half is asserted by ``deploy/Caddyfile`` and
documented in deploy/README.md.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from uvicorn.config import Config
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api.v1.auth import _client_ip

#: What deploy/app.env.example sets, and what the assertions below mean.
CADDY_ADDRESS = "10.89.61.20"
GATEWAY_ADDRESS = "10.89.61.1"
DEPLOYED_ALLOW_IPS = f"{CADDY_ADDRESS},{GATEWAY_ADDRESS}"


def _probe(trusted: str, peer: str) -> TestClient:
    """The rate limiter's key, behind uvicorn's proxy-header handling.

    ``client=`` sets the ASGI scope's peer address, which is what the
    middleware's trust check reads — the same thing the socket layer
    would supply in production.
    """
    app = FastAPI()

    @app.get("/whoami")
    def whoami(request: Request) -> dict[str, str]:
        return {"key": _client_ip(request)}

    # Both casts are the usual ASGI-protocol mismatch between Starlette's
    # loose MutableMapping scope and uvicorn's TypedDict one; they are the
    # same object at runtime.
    wrapped = ProxyHeadersMiddleware(cast("Any", app), trusted_hosts=trusted)
    return TestClient(cast("Any", wrapped), client=(peer, 40000))


def _key(trusted: str, *, peer: str, forwarded: str | None) -> str:
    headers = {"X-Forwarded-For": forwarded} if forwarded is not None else {}
    response = _probe(trusted, peer).get("/whoami", headers=headers)
    return str(response.json()["key"])


# --- uvicorn's defaults, which the deployment relies on ------------------


def test_proxy_header_handling_is_on_by_default() -> None:
    """The Containerfile's CMD passes no proxy flags: it relies on this."""
    config = Config(app=FastAPI())
    assert config.proxy_headers is True


@pytest.fixture
def _no_env_allow_ips() -> Iterator[None]:
    previous = os.environ.pop("FORWARDED_ALLOW_IPS", None)
    yield
    if previous is not None:
        os.environ["FORWARDED_ALLOW_IPS"] = previous


@pytest.mark.usefixtures("_no_env_allow_ips")
def test_the_trusted_peer_list_comes_from_the_environment() -> None:
    """FORWARDED_ALLOW_IPS is set in app.env, not on the command line."""
    assert Config(app=FastAPI()).forwarded_allow_ips == "127.0.0.1"

    os.environ["FORWARDED_ALLOW_IPS"] = DEPLOYED_ALLOW_IPS
    assert Config(app=FastAPI()).forwarded_allow_ips == DEPLOYED_ALLOW_IPS


# --- the chain, as deployed ---------------------------------------------


def test_an_untrusted_peer_cannot_choose_its_own_bucket() -> None:
    """The property that makes reading the header safe at all."""
    key = _key(DEPLOYED_ALLOW_IPS, peer="198.51.100.5", forwarded="203.0.113.9")
    assert key == "198.51.100.5"


def test_the_deployed_chain_resolves_to_the_real_client() -> None:
    """`<real client>, <gateway>` is what Caddy sends once it is
    configured to append; uvicorn must walk past the gateway."""
    key = _key(DEPLOYED_ALLOW_IPS, peer=CADDY_ADDRESS, forwarded=f"203.0.113.9, {GATEWAY_ADDRESS}")
    assert key == "203.0.113.9"


def test_omitting_the_gateway_collapses_every_client_into_one_bucket() -> None:
    """The failure mode, demonstrated. This is what the old single-address
    FORWARDED_ALLOW_IPS produced: a rate limit shared by the internet."""
    first = _key(CADDY_ADDRESS, peer=CADDY_ADDRESS, forwarded=f"203.0.113.9, {GATEWAY_ADDRESS}")
    second = _key(CADDY_ADDRESS, peer=CADDY_ADDRESS, forwarded=f"198.51.100.7, {GATEWAY_ADDRESS}")
    assert first == second == GATEWAY_ADDRESS


def test_a_client_supplied_hop_cannot_shift_the_bucket() -> None:
    """A client that writes its own X-Forwarded-For gets it appended to,
    not honoured: the rightmost untrusted entry is still the address the
    outer proxy vouched for."""
    forged = _key(
        DEPLOYED_ALLOW_IPS,
        peer=CADDY_ADDRESS,
        forwarded=f"10.0.0.99, 203.0.113.9, {GATEWAY_ADDRESS}",
    )
    assert forged == "203.0.113.9"


def test_wildcard_trust_would_let_a_caller_pick_its_bucket() -> None:
    """Why deploy/app.env.example says never `*`: the wildcard branch
    takes the leftmost value, which is whatever the client wrote."""
    key = _key("*", peer=CADDY_ADDRESS, forwarded=f"10.0.0.99, 203.0.113.9, {GATEWAY_ADDRESS}")
    assert key == "10.0.0.99"


def test_no_forwarded_header_falls_back_to_the_peer() -> None:
    """An outer proxy that sets nothing: every request buckets under
    Caddy. Correct, and the reason the README says to check."""
    assert _key(DEPLOYED_ALLOW_IPS, peer=CADDY_ADDRESS, forwarded=None) == CADDY_ADDRESS


# --- the documented configuration and the code agree --------------------


def test_the_deployment_template_lists_both_hops() -> None:
    """Drift between app.env.example and this reasoning is silent."""
    text = Path(__file__).resolve().parent.parent.joinpath("deploy/app.env.example").read_text()
    line = next(row for row in text.splitlines() if row.startswith("FORWARDED_ALLOW_IPS="))
    configured = {part.strip() for part in line.split("=", 1)[1].split(",")}
    assert configured == {CADDY_ADDRESS, GATEWAY_ADDRESS}


def test_caddy_is_configured_to_append_rather_than_replace() -> None:
    """Without `trusted_proxies`, Caddy discards the outer proxy's
    X-Forwarded-For and the chain above never forms."""
    caddyfile = Path(__file__).resolve().parent.parent.joinpath("deploy/Caddyfile").read_text()
    assert "trusted_proxies static" in caddyfile
    assert GATEWAY_ADDRESS in caddyfile
