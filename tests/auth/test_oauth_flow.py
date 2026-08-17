"""The GitHub authorization-code flow, end to end against a mocked GitHub."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
import time_machine
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.auth import callback_failure_limiter, start_limiter
from app.auth import github
from app.auth.state import STATE_COOKIE_NAME
from app.db.models import User, UserPreferences, UserSession
from app.security.tokens import hash_session_token
from app.settings import Settings
from tests.auth.conftest import (
    ACCESS_TOKEN,
    ALLOWED_GITHUB_ID,
    CALLBACK_PATH,
    OAUTH_CODE,
    START_PATH,
    TOKEN_ROUTE,
    USER_ROUTE,
    SignIn,
    state_from_location,
)


def test_start_redirects_to_github_with_the_exact_redirect_uri(
    client: TestClient, settings: Settings
) -> None:
    response = client.get(START_PATH, follow_redirects=False)

    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == github.AUTHORIZE_URL

    query = parse_qs(location.query)
    assert query["client_id"] == [settings.github_client_id]
    # Exact match, not a prefix or a normalised variant: GitHub's own
    # redirect-URI check is only as good as the value we send.
    assert query["redirect_uri"] == [settings.github_redirect_uri]
    assert query["scope"] == [github.OAUTH_SCOPE]
    assert query["state"][0]

    # State is bound to this browser for the length of the flow.
    assert client.cookies[STATE_COOKIE_NAME] == query["state"][0]


def test_client_secret_never_reaches_the_browser(client: TestClient, settings: Settings) -> None:
    response = client.get(START_PATH, follow_redirects=False)
    secret = settings.github_client_secret.get_secret_value()
    assert secret not in response.headers["location"]
    assert secret not in response.text
    assert secret not in str(dict(response.headers))


def test_callback_creates_the_user_and_issues_a_session(
    client: TestClient,
    settings: Settings,
    db_session: Session,
    github_api: respx.MockRouter,
    ensure_profile_calls: list[int],
    sign_in: SignIn,
) -> None:
    response = sign_in()

    assert response.status_code == 302
    assert response.headers["location"] == settings.app_base_url.rstrip("/") + "/"

    user = db_session.execute(select(User).where(User.github_id == ALLOWED_GITHUB_ID)).scalar_one()
    assert user.github_login == "octocat"
    assert user.display_name == "Octo Cat"
    assert ensure_profile_calls == [user.id]

    # Session and CSRF cookies both issued; the state cookie is spent.
    assert client.cookies.get(settings.session_cookie_name)
    assert client.cookies.get(settings.csrf_cookie_name)
    assert client.cookies.get(STATE_COOKIE_NAME) is None


def test_session_cookie_carries_the_required_flags(
    client: TestClient, settings: Settings, github_api: respx.MockRouter, sign_in: SignIn
) -> None:
    response = sign_in()

    session_cookie = next(
        value
        for key, value in response.headers.multi_items()
        if key.lower() == "set-cookie" and value.startswith(f"{settings.session_cookie_name}=")
    )
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "samesite=lax" in session_cookie.lower()

    csrf_cookie = next(
        value
        for key, value in response.headers.multi_items()
        if key.lower() == "set-cookie" and value.startswith(f"{settings.csrf_cookie_name}=")
    )
    # Deliberately readable: the frontend must echo it in the header.
    assert "HttpOnly" not in csrf_cookie
    assert "Secure" in csrf_cookie


def test_only_the_session_token_hash_is_stored(
    client: TestClient,
    settings: Settings,
    db_session: Session,
    github_api: respx.MockRouter,
    sign_in: SignIn,
) -> None:
    sign_in()
    raw = client.cookies[settings.session_cookie_name]
    stored = db_session.execute(select(UserSession.token_hash)).scalars().all()

    assert stored == [hash_session_token(raw)]
    assert raw not in stored


def test_code_exchange_posts_the_secret_and_exact_redirect_uri(
    settings: Settings, github_api: respx.MockRouter, sign_in: SignIn
) -> None:
    sign_in()

    request = github_api[TOKEN_ROUTE].calls.last.request
    body = parse_qs(request.content.decode())
    assert body["code"] == [OAUTH_CODE]
    assert body["client_id"] == [settings.github_client_id]
    assert body["client_secret"] == [settings.github_client_secret.get_secret_value()]
    assert body["redirect_uri"] == [settings.github_redirect_uri]

    profile_request = github_api[USER_ROUTE].calls.last.request
    assert profile_request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"


def test_repeat_sign_in_updates_the_profile_without_duplicating_the_user(
    client: TestClient,
    db_session: Session,
    github_api: respx.MockRouter,
    ensure_profile_calls: list[int],
    sign_in: SignIn,
) -> None:
    """Acceptance criterion 1: one GitHub account, one user row, ever."""
    assert sign_in().status_code == 302

    github_api[USER_ROUTE].mock(
        return_value=httpx.Response(
            200,
            json={
                "id": ALLOWED_GITHUB_ID,
                "login": "renamed-octocat",
                "name": "Renamed Cat",
                "avatar_url": "https://avatars.example/renamed.png",
            },
        )
    )
    assert sign_in().status_code == 302

    assert db_session.execute(select(func.count()).select_from(User)).scalar_one() == 1
    user = db_session.execute(select(User)).scalar_one()
    db_session.refresh(user)
    assert user.github_login == "renamed-octocat"
    assert user.display_name == "Renamed Cat"
    assert user.avatar_url == "https://avatars.example/renamed.png"
    assert ensure_profile_calls == [user.id, user.id]


def test_replayed_state_is_rejected(
    client: TestClient, github_api: respx.MockRouter, ensure_profile_calls: list[int]
) -> None:
    start = client.get(START_PATH, follow_redirects=False)
    state = state_from_location(start.headers["location"])
    params = {"code": OAUTH_CODE, "state": state}

    assert client.get(CALLBACK_PATH, params=params, follow_redirects=False).status_code == 302

    # The callback clears the state cookie, so a genuine replay could not
    # even reach the store. Put it back to prove the store itself refuses.
    client.cookies.set(STATE_COOKIE_NAME, state, path="/api/v1/auth")
    replay = client.get(CALLBACK_PATH, params=params, follow_redirects=False)
    assert replay.status_code == 403


def test_expired_state_is_rejected(
    client: TestClient, github_api: respx.MockRouter, ensure_profile_calls: list[int]
) -> None:
    with time_machine.travel("2026-08-17 09:00:00 +0000", tick=False) as traveller:
        start = client.get(START_PATH, follow_redirects=False)
        state = state_from_location(start.headers["location"])
        traveller.shift(3600)
        response = client.get(
            CALLBACK_PATH,
            params={"code": OAUTH_CODE, "state": state},
            follow_redirects=False,
        )

    assert response.status_code == 403


def test_state_from_another_browser_is_rejected(
    client: TestClient, github_api: respx.MockRouter, ensure_profile_calls: list[int]
) -> None:
    """The state cookie binds redemption to the browser that started the
    flow, so a state harvested elsewhere cannot be planted here."""
    start = client.get(START_PATH, follow_redirects=False)
    state = state_from_location(start.headers["location"])
    client.cookies.delete(STATE_COOKIE_NAME, path="/api/v1/auth")

    response = client.get(
        CALLBACK_PATH, params={"code": OAUTH_CODE, "state": state}, follow_redirects=False
    )
    assert response.status_code == 403


def test_forged_state_is_rejected(client: TestClient, github_api: respx.MockRouter) -> None:
    client.get(START_PATH, follow_redirects=False)
    response = client.get(
        CALLBACK_PATH,
        params={"code": OAUTH_CODE, "state": "forged.9999999999.deadbeef"},
        follow_redirects=False,
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("route", "response"),
    [
        pytest.param(TOKEN_ROUTE, httpx.Response(401), id="token-endpoint-rejects"),
        pytest.param(
            TOKEN_ROUTE,
            httpx.Response(200, json={"error": "bad_verification_code"}),
            id="token-endpoint-reports-error-with-200",
        ),
        pytest.param(USER_ROUTE, httpx.Response(500), id="user-endpoint-fails"),
        pytest.param(
            USER_ROUTE,
            httpx.Response(200, json={"login": "no-id"}),
            id="profile-without-a-numeric-id",
        ),
    ],
)
def test_github_failures_deny_sign_in(
    db_session: Session,
    github_api: respx.MockRouter,
    sign_in: SignIn,
    route: str,
    response: httpx.Response,
) -> None:
    github_api[route].mock(return_value=response)

    result = sign_in()

    assert result.status_code == 502
    assert db_session.execute(select(func.count()).select_from(User)).scalar_one() == 0


def test_a_failing_preference_service_leaves_no_user_row(
    db_session: Session,
    github_api: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    sign_in: SignIn,
) -> None:
    """Sign-in is one transaction from the allow-list check to the session
    insert, so a failure in the preference service — agent C's module,
    reached after the user row is written — cannot leave a half-created
    account behind."""

    def _explode(db: Session, user: User) -> None:
        raise RuntimeError("preference service unavailable")

    monkeypatch.setattr("app.services.preferences.ensure_profile", _explode)

    with pytest.raises(RuntimeError):
        sign_in()

    assert db_session.execute(select(func.count()).select_from(User)).scalar_one() == 0


def test_sign_in_creates_the_preference_profile_through_the_service(
    db_session: Session, github_api: respx.MockRouter, sign_in: SignIn
) -> None:
    """The A/C seam, unstubbed: the real ``ensure_profile`` runs inside the
    callback's transaction and its rows are committed with the user."""
    assert sign_in().status_code == 302

    user_id = db_session.execute(select(User.id)).scalar_one()
    profile = db_session.execute(
        select(UserPreferences).where(UserPreferences.user_id == user_id)
    ).scalar_one()
    assert profile.user_id == user_id


def test_oauth_start_is_rate_limited(client: TestClient) -> None:
    statuses = [
        client.get(START_PATH, follow_redirects=False).status_code
        for _ in range(start_limiter.limit + 1)
    ]
    assert statuses[: start_limiter.limit] == [302] * start_limiter.limit
    assert statuses[-1] == 429


def test_repeated_callback_failures_are_rate_limited(
    client: TestClient, github_api: respx.MockRouter
) -> None:
    callback_failure_limiter.reset()
    params = {"code": OAUTH_CODE, "state": "forged.9999999999.deadbeef"}

    statuses = [
        client.get(CALLBACK_PATH, params=params, follow_redirects=False).status_code
        for _ in range(12)
    ]
    assert statuses[0] == 403
    assert statuses[-1] == 429


def test_successful_sign_in_is_not_throttled_by_the_failure_limiter(
    client: TestClient, github_api: respx.MockRouter, ensure_profile_calls: list[int]
) -> None:
    """Only failures count, so a busy instance signing people in is never
    throttled on the callback."""
    for _ in range(5):
        start = client.get(START_PATH, follow_redirects=False)
        response = client.get(
            CALLBACK_PATH,
            params={
                "code": OAUTH_CODE,
                "state": state_from_location(start.headers["location"]),
            },
            follow_redirects=False,
        )
        assert response.status_code == 302


def test_profile_only_scope_requested(client: TestClient) -> None:
    location = client.get(START_PATH, follow_redirects=False).headers["location"]
    scopes = parse_qs(urlparse(location).query)["scope"][0].split()
    assert scopes == ["read:user"]
    assert not any(scope.startswith(("repo", "admin", "delete")) for scope in scopes)
