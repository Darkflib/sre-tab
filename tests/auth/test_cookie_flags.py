"""Cookie flags, and the one setting that relaxes exactly one of them.

``Secure`` was unconditional, which is right everywhere except one case:
development against a host that is not ``localhost``, over plain http.
Browsers exempt localhost from the Secure rule, so ordinary local work
never needed an override — but a colleague pointing a browser at
``http://dev-box.lan`` gets every cookie silently dropped, and that reads
as a broken sign-in rather than as a configuration choice.

``COOKIE_SECURE`` is therefore a setting rather than a guess about the
environment. It defaults to true, it is the only flag it touches, and the
tests below pin both halves of that.
"""

from __future__ import annotations

from http.cookies import SimpleCookie

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.auth.state import STATE_COOKIE_NAME
from app.db.engine import create_db_engine
from app.db.models import Base
from app.main import create_app
from app.settings import Settings
from tests.auth.conftest import START_PATH, SignIn


def _cookie(response_headers_raw: list[tuple[bytes, bytes]], name: str) -> SimpleCookie:
    """Parse the raw ``Set-Cookie`` for *name* out of a response.

    httpx's cookie jar keeps values, not attributes, so the flags have to
    be read off the header itself.
    """
    for key, value in response_headers_raw:
        if key.lower() != b"set-cookie":
            continue
        jar = SimpleCookie()
        jar.load(value.decode("latin-1"))
        if name in jar:
            return jar
    raise AssertionError(f"no Set-Cookie for {name!r}")


def test_session_and_csrf_cookies_are_secure_by_default(
    client: TestClient, settings: Settings, github_api: respx.MockRouter, sign_in: SignIn
) -> None:
    response = sign_in()
    assert response.status_code == 302

    session = _cookie(response.headers.raw, settings.session_cookie_name)[
        settings.session_cookie_name
    ]
    assert session["secure"]
    assert session["httponly"]
    assert session["samesite"].lower() == "lax"

    # Not HttpOnly by design: the double-submit pattern needs the
    # frontend to read it. Still Secure.
    csrf = _cookie(response.headers.raw, settings.csrf_cookie_name)[settings.csrf_cookie_name]
    assert csrf["secure"]
    assert not csrf["httponly"]


def test_the_oauth_state_cookie_is_secure_by_default(client: TestClient) -> None:
    response = client.get(START_PATH, follow_redirects=False)
    state = _cookie(response.headers.raw, STATE_COOKIE_NAME)[STATE_COOKIE_NAME]
    assert state["secure"]
    assert state["httponly"]


def _http_client(settings: Settings, *, cookie_secure: bool) -> TestClient:
    """A client over plain http against a non-localhost host.

    ``httpx``'s jar applies the same rule a browser does — a ``Secure``
    cookie is never sent over http — so these assertions are the
    setting's actual behaviour rather than a proxy for it.
    """
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    configured = settings.model_copy(update={"cookie_secure": cookie_secure})
    return TestClient(create_app(configured, engine=engine), base_url="http://dev-box.lan")


@pytest.fixture
def insecure_client(settings: Settings) -> TestClient:
    return _http_client(settings, cookie_secure=False)


def test_cookie_secure_false_drops_only_the_secure_flag(insecure_client: TestClient) -> None:
    """HttpOnly and SameSite are not part of the bargain."""
    response = insecure_client.get(START_PATH, follow_redirects=False)
    state = _cookie(response.headers.raw, STATE_COOKIE_NAME)[STATE_COOKIE_NAME]
    assert not state["secure"]
    assert state["httponly"]
    assert state["samesite"].lower() == "lax"


def test_the_default_settings_object_is_secure() -> None:
    """The default has to be safe, because the override is the exception
    and nothing in the deployment sets it."""
    assert Settings(_env_file=None).cookie_secure is True  # type: ignore[call-arg]


def _replayed_over_http(client: TestClient) -> str:
    """What the cookie jar would send back on the next plain-http request.

    The Secure attribute is enforced on *return*, not on storage: the
    cookie is accepted and then never sent again. That is precisely why
    the failure is so opaque in a browser — the jar shows the cookie, the
    request does not carry it, and the server sees an unbound flow.
    """
    request = httpx.Request("GET", "http://dev-box.lan/api/v1/auth/github/callback")
    client.cookies.set_cookie_header(request)
    return str(request.headers.get("cookie", ""))


def test_a_plain_http_client_never_replays_a_secure_cookie(settings: Settings) -> None:
    """The failure the setting exists for, demonstrated rather than
    described: the state cookie is never sent back, so the flow cannot
    complete and nothing in the logs says why."""
    strict = _http_client(settings, cookie_secure=True)
    strict.get(START_PATH, follow_redirects=False)
    assert STATE_COOKIE_NAME not in _replayed_over_http(strict)


def test_a_plain_http_client_replays_the_cookie_when_relaxed(
    insecure_client: TestClient,
) -> None:
    insecure_client.get(START_PATH, follow_redirects=False)
    assert STATE_COOKIE_NAME in _replayed_over_http(insecure_client)
