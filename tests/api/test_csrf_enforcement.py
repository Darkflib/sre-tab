"""Every mutating API route, exercised with a real session cookie.

``tests/conftest.py`` overrides ``get_current_user``, so ``authed_client``
never sends a session cookie — and ``CSRFMiddleware`` only fires when one
is present. That is the right trade for the rest of ``tests/api``, which
is about what the routes *do*, but it means the whole directory ran with
CSRF silently unenforced and could not have noticed if it stopped
working.

Nothing here uses the override. The session is a real row created the way
:func:`app.auth.sessions.create_session` creates it, and the CSRF cookie
is bound to it exactly as the OAuth callback binds it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.sessions import create_session
from app.db.models import User
from app.security.csrf import generate_csrf_token
from app.settings import Settings
from tests.api.conftest import Catalogue


@dataclass(frozen=True)
class RealSession:
    """A client holding a genuine session and its bound CSRF token."""

    client: TestClient
    session_token: str
    csrf_token: str


def _open_session(client: TestClient, db: Session, user: User, settings: Settings) -> RealSession:
    session_token = create_session(db, user, settings)
    db.commit()
    csrf_token = generate_csrf_token(settings.session_secret.get_secret_value(), session_token)
    client.cookies.set(settings.session_cookie_name, session_token)
    client.cookies.set(settings.csrf_cookie_name, csrf_token)
    return RealSession(client=client, session_token=session_token, csrf_token=csrf_token)


@pytest.fixture
def real_session(
    app: FastAPI,
    client: TestClient,
    db_session: Session,
    test_user: User,
    settings: Settings,
) -> Iterator[RealSession]:
    """Deliberately does not touch ``app.dependency_overrides``."""
    assert not app.dependency_overrides, "this suite must run against the real dependency"
    yield _open_session(client, db_session, test_user, settings)


def _mutations(catalogue: Catalogue) -> list[tuple[str, str, str, dict[str, Any]]]:
    """One mutating endpoint per API module. Logout goes last: it is the
    only one that ends the session the others need."""
    item_id = catalogue.items["lobsters"]
    return [
        ("me", "PATCH", "/api/v1/me/preferences", {"json": {"theme": "dark"}}),
        ("items", "PUT", f"/api/v1/items/{item_id}/read-state", {"json": {"read": True}}),
        ("bookmarks-put", "PUT", f"/api/v1/items/{item_id}/bookmark", {}),
        ("bookmarks-delete", "DELETE", f"/api/v1/items/{item_id}/bookmark", {}),
        ("auth", "POST", "/api/v1/auth/logout", {}),
    ]


@pytest.fixture
def mutations(catalogue: Catalogue) -> list[tuple[str, str, str, dict[str, Any]]]:
    return _mutations(catalogue)


def test_every_module_refuses_a_mutation_without_the_csrf_header(
    real_session: RealSession, mutations: list[tuple[str, str, str, dict[str, Any]]]
) -> None:
    for name, method, path, kwargs in mutations:
        response = real_session.client.request(method, path, **kwargs)
        assert response.status_code == 403, f"{name}: {response.status_code}"
        assert response.json()["detail"] == "CSRF validation failed", name


def test_every_module_accepts_the_bound_pair(
    real_session: RealSession,
    settings: Settings,
    mutations: list[tuple[str, str, str, dict[str, Any]]],
) -> None:
    """The check passes and the route actually runs — a suite that only
    asserted the refusals would pass with CSRF wired to reject always."""
    headers = {settings.csrf_header_name: real_session.csrf_token}
    for name, method, path, kwargs in mutations:
        response = real_session.client.request(method, path, headers=headers, **kwargs)
        assert response.status_code < 400, f"{name}: {response.status_code} {response.text}"


def test_a_token_bound_to_another_session_is_refused(
    real_session: RealSession,
    db_session: Session,
    second_user: User,
    settings: Settings,
    mutations: list[tuple[str, str, str, dict[str, Any]]],
) -> None:
    """A validly signed token that belongs to somebody else's session.

    This is the reproduced attack, at the API surface, with both sessions
    real: before the binding it returned 200.
    """
    other_token = create_session(db_session, second_user, settings)
    db_session.commit()
    foreign = generate_csrf_token(settings.session_secret.get_secret_value(), other_token)
    real_session.client.cookies.set(settings.csrf_cookie_name, foreign)

    for name, method, path, kwargs in mutations:
        response = real_session.client.request(
            method, path, headers={settings.csrf_header_name: foreign}, **kwargs
        )
        assert response.status_code == 403, f"{name}: {response.status_code}"


def test_a_revoked_session_cannot_mutate(
    real_session: RealSession, settings: Settings, catalogue: Catalogue
) -> None:
    """Logout revokes the row; the CSRF pair in the jar is now inert
    because there is no session behind it to authenticate."""
    headers = {settings.csrf_header_name: real_session.csrf_token}
    assert real_session.client.post("/api/v1/auth/logout", headers=headers).status_code == 204

    real_session.client.cookies.set(settings.session_cookie_name, real_session.session_token)
    real_session.client.cookies.set(settings.csrf_cookie_name, real_session.csrf_token)
    response = real_session.client.patch(
        "/api/v1/me/preferences", json={"theme": "dark"}, headers=headers
    )
    assert response.status_code == 401


def test_safe_methods_are_not_challenged(real_session: RealSession) -> None:
    assert real_session.client.get("/api/v1/me").status_code == 200
    assert real_session.client.get("/api/v1/feed").status_code == 200
    assert real_session.client.get("/api/v1/bookmarks").status_code == 200
    assert real_session.client.get("/api/v1/sources").status_code == 200
