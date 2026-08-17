"""CSRF enforcement is structural, not per-route.

The point of testing agent C's endpoints here is that agent A wrote no
line of them: enforcement lives in middleware, so every mutating route in
the API inherits it.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.security.csrf import generate_csrf_token
from app.settings import Settings
from tests.auth.conftest import PreferencesStub, SignIn, csrf_headers

# Mutating endpoints across the whole API. The last three belong to agent
# C and are untouched by this agent.
MUTATING: list[tuple[str, str, dict[str, Any]]] = [
    ("POST", "/api/v1/auth/logout", {}),
    ("PATCH", "/api/v1/me/preferences", {"json": {}}),
    ("DELETE", "/api/v1/me", {}),
    ("PUT", "/api/v1/items/1/read-state", {"json": {"read": True}}),
    ("PUT", "/api/v1/items/1/bookmark", {}),
    ("DELETE", "/api/v1/items/1/bookmark", {}),
]


@pytest.mark.parametrize(("method", "path", "kwargs"), MUTATING)
def test_mutating_request_without_the_csrf_header_is_refused(
    signed_in_client: TestClient, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    response = signed_in_client.request(method, path, **kwargs)
    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF validation failed"


@pytest.mark.parametrize(("method", "path", "kwargs"), MUTATING)
def test_mutating_request_with_the_matching_pair_passes_the_check(
    signed_in_client: TestClient,
    settings: Settings,
    preferences_stub: PreferencesStub,
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    """Whatever the route then answers, it is no longer a CSRF refusal."""
    response = signed_in_client.request(
        method, path, headers=csrf_headers(signed_in_client, settings), **kwargs
    )
    assert response.status_code != 403


def test_a_validly_signed_but_mismatched_header_is_refused(
    signed_in_client: TestClient, settings: Settings
) -> None:
    """Double submit: the two halves must match, not merely both verify."""
    session_token = signed_in_client.cookies[settings.session_cookie_name]
    other = generate_csrf_token(settings.session_secret.get_secret_value(), session_token)
    response = signed_in_client.post(
        "/api/v1/auth/logout", headers={settings.csrf_header_name: other}
    )
    assert response.status_code == 403


def test_a_header_signed_with_another_secret_is_refused(
    signed_in_client: TestClient, settings: Settings
) -> None:
    session_token = signed_in_client.cookies[settings.session_cookie_name]
    forged = generate_csrf_token("an-attacker-controlled-secret", session_token)
    signed_in_client.cookies.set(settings.csrf_cookie_name, forged)
    response = signed_in_client.post(
        "/api/v1/auth/logout", headers={settings.csrf_header_name: forged}
    )
    assert response.status_code == 403


def test_a_token_minted_for_a_different_session_is_refused(
    signed_in_client: TestClient, settings: Settings
) -> None:
    """The end-to-end form of the reproduced attack: a token our own
    server would sign, presented against somebody else's session."""
    foreign = generate_csrf_token(
        settings.session_secret.get_secret_value(), "a-session-that-is-not-this-one"
    )
    signed_in_client.cookies.set(settings.csrf_cookie_name, foreign)
    response = signed_in_client.patch(
        "/api/v1/me/preferences", json={}, headers={settings.csrf_header_name: foreign}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF validation failed"


def test_the_csrf_cookie_does_not_survive_a_session_rotation(
    signed_in_client: TestClient, settings: Settings, sign_in: SignIn
) -> None:
    """Signing in again revokes the old session and issues a new pair.

    The previous CSRF token was minted against the previous session, so
    it stops verifying at the moment that session stops existing — the
    binding tracks rotation for free.
    """
    stale = signed_in_client.cookies[settings.csrf_cookie_name]
    assert sign_in().status_code == 302

    signed_in_client.cookies.set(settings.csrf_cookie_name, stale)
    response = signed_in_client.post(
        "/api/v1/auth/logout", headers={settings.csrf_header_name: stale}
    )
    assert response.status_code == 403


def test_safe_methods_are_never_challenged(
    signed_in_client: TestClient, preferences_stub: PreferencesStub
) -> None:
    assert signed_in_client.get("/api/v1/healthz").status_code == 200
    assert signed_in_client.get("/api/v1/me").status_code == 200
    assert signed_in_client.get("/api/v1/sources").status_code != 403


def test_the_oauth_callback_is_not_challenged(signed_in_client: TestClient) -> None:
    """A top-level cross-site GET navigation carries no CSRF header and
    must still be able to arrive."""
    response = signed_in_client.get(
        "/api/v1/auth/github/callback",
        params={"code": "x", "state": "y"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert response.json()["detail"] != "CSRF validation failed"


def test_unauthenticated_mutation_is_401_not_403(client: TestClient) -> None:
    """Without the session cookie there is no ambient authority to forge,
    so the check does not fire and the request gets the honest answer."""
    assert client.delete("/api/v1/me").status_code == 401
