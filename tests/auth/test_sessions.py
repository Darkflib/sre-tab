"""Session resolution, revocation, expiry, and rotation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.sessions import resolve_session
from app.db.models import User, UserSession
from app.security.tokens import generate_session_token, hash_session_token
from app.settings import Settings
from tests.auth.conftest import SignIn, csrf_headers

ME_PATH = "/api/v1/me"
LOGOUT_PATH = "/api/v1/auth/logout"


def _store_session(db: Session, user: User, **overrides: object) -> str:
    token = generate_session_token()
    values: dict[str, object] = {
        "user_id": user.id,
        "token_hash": hash_session_token(token),
        "expires_at": datetime.now(UTC) + timedelta(days=1),
    }
    values.update(overrides)
    db.add(UserSession(**values))
    db.commit()
    return token


def test_live_session_resolves_to_its_owner(db_session: Session, test_user: User) -> None:
    token = _store_session(db_session, test_user)
    resolved = resolve_session(db_session, token)
    assert resolved is not None
    assert resolved.id == test_user.id


def test_expired_session_does_not_resolve(db_session: Session, test_user: User) -> None:
    token = _store_session(
        db_session, test_user, expires_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    assert resolve_session(db_session, token) is None


def test_revoked_session_does_not_resolve(db_session: Session, test_user: User) -> None:
    token = _store_session(db_session, test_user, revoked_at=datetime.now(UTC))
    assert resolve_session(db_session, token) is None


def test_unknown_token_does_not_resolve(db_session: Session, test_user: User) -> None:
    _store_session(db_session, test_user)
    assert resolve_session(db_session, generate_session_token()) is None


def test_no_cookie_is_401(client: TestClient) -> None:
    assert client.get(ME_PATH).status_code == 401


def test_forged_cookie_is_401(client: TestClient, settings: Settings) -> None:
    client.cookies.set(settings.session_cookie_name, generate_session_token())
    assert client.get(ME_PATH).status_code == 401


def test_expired_cookie_is_401(
    client: TestClient, settings: Settings, db_session: Session, test_user: User
) -> None:
    token = _store_session(db_session, test_user, expires_at=datetime.now(UTC) - timedelta(days=1))
    client.cookies.set(settings.session_cookie_name, token)
    assert client.get(ME_PATH).status_code == 401


def test_logout_revokes_server_side_and_clears_the_cookie(
    signed_in_client: TestClient, settings: Settings, db_session: Session
) -> None:
    token = signed_in_client.cookies[settings.session_cookie_name]

    response = signed_in_client.post(LOGOUT_PATH, headers=csrf_headers(signed_in_client, settings))
    assert response.status_code == 204

    stored = db_session.execute(
        select(UserSession).where(UserSession.token_hash == hash_session_token(token))
    ).scalar_one()
    db_session.refresh(stored)
    assert stored.revoked_at is not None

    # Cleared client-side, and dead server-side even if the raw token is
    # replayed by something that kept a copy.
    assert not signed_in_client.cookies.get(settings.session_cookie_name)
    signed_in_client.cookies.set(settings.session_cookie_name, token)
    assert signed_in_client.get(ME_PATH).status_code == 401


def test_sign_in_rotates_the_session_token(
    signed_in_client: TestClient,
    settings: Settings,
    db_session: Session,
    github_api: respx.MockRouter,
    sign_in: SignIn,
) -> None:
    first = signed_in_client.cookies[settings.session_cookie_name]

    assert sign_in().status_code == 302
    second = signed_in_client.cookies[settings.session_cookie_name]
    assert second != first

    superseded = db_session.execute(
        select(UserSession).where(UserSession.token_hash == hash_session_token(first))
    ).scalar_one()
    db_session.refresh(superseded)
    assert superseded.revoked_at is not None
    assert resolve_session(db_session, first) is None
    assert resolve_session(db_session, second) is not None
