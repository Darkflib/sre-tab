"""Fixtures for the auth suite.

Root fixtures come from tests/conftest.py (Phase 0 property); everything
here is additive.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.api.v1.auth import callback_failure_limiter, start_limiter
from app.api.v1.schemas import PreferencesOut, PreferencesPatch
from app.auth import api_tokens, github
from app.auth.state import state_store
from app.db.models import ApiTokenScope, Layout, Theme, User
from app.main import create_app
from app.settings import Settings

# Fixed fakes, distinctive enough to grep for in captured log output.
OAUTH_CODE = "oauth-code-must-never-be-logged"
ACCESS_TOKEN = "gho_accesstokenmustneverbelogged00"
ALLOWED_GITHUB_ID = 1000001
DENIED_GITHUB_ID = 2000002
#: A second allow-listed identity. Distinct from DENIED_GITHUB_ID:
#: the cross-user tests need two accounts that may both sign in.
SECOND_GITHUB_ID = 1000002

START_PATH = "/api/v1/auth/github/start"
CALLBACK_PATH = "/api/v1/auth/github/callback"

TOKEN_ROUTE = "github_token"
USER_ROUTE = "github_user"


def state_from_location(location: str) -> str:
    """Pull the ``state`` GitHub would echo back out of the redirect."""
    return parse_qs(urlparse(location).query)["state"][0]


def github_profile(github_id: int = ALLOWED_GITHUB_ID, login: str = "octocat") -> dict[str, object]:
    return {
        "id": github_id,
        "login": login,
        "name": "Octo Cat",
        "avatar_url": "https://avatars.example/octocat.png",
    }


@pytest.fixture(autouse=True)
def _isolate_process_state() -> Iterator[None]:
    """The state store and rate limiters are process-wide singletons, as
    the single-instance v1 deployment intends. Tests must not inherit each
    other's budgets or nonces."""
    for singleton in (state_store, start_limiter, callback_failure_limiter):
        singleton.reset()
    yield
    for singleton in (state_store, start_limiter, callback_failure_limiter):
        singleton.reset()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Overrides the root fixture with an **https** base URL.

    The session and CSRF cookies are ``Secure``, and httpx's cookie jar
    correctly refuses to replay a Secure cookie over plain http — so an
    http test client would silently never authenticate. Driving the suite
    over https is also what production actually looks like.
    """
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


@pytest.fixture
def ensure_profile_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Stub agent C's ``ensure_profile`` and record the user IDs it was
    called with, so the seam can be asserted without depending on that
    module's behaviour. ``test_oauth_flow`` exercises the real one too."""
    calls: list[int] = []

    def _stub(db: Session, user: User) -> None:
        calls.append(user.id)

    monkeypatch.setattr("app.services.preferences.ensure_profile", _stub)
    return calls


DEFAULT_PROFILE = PreferencesOut(
    theme=Theme.SYSTEM,
    layout=Layout.GRID,
    max_visible_cards=25,
    onboarding_completed=False,
    topics=[],
    sources=[],
    muted_words=[],
    muted_tags=[],
)


@dataclass
class PreferencesStub:
    """Stands in for agent C's service, so these tests assert what agent
    A's routes do with it — delegation, and the ``ValueError`` to 422
    mapping — rather than re-testing agent C's behaviour."""

    loaded: list[int] = field(default_factory=list)
    patches: list[PreferencesPatch] = field(default_factory=list)
    patch_error: str | None = None

    def load_profile(self, db: Session, user: User) -> PreferencesOut:
        self.loaded.append(user.id)
        return DEFAULT_PROFILE

    def apply_patch(self, db: Session, user: User, patch: PreferencesPatch) -> PreferencesOut:
        if self.patch_error is not None:
            raise ValueError(self.patch_error)
        self.patches.append(patch)
        return DEFAULT_PROFILE.model_copy(update=patch.model_dump(exclude_none=True))


@pytest.fixture
def preferences_stub(monkeypatch: pytest.MonkeyPatch) -> PreferencesStub:
    stub = PreferencesStub()
    monkeypatch.setattr("app.services.preferences.load_profile", stub.load_profile)
    monkeypatch.setattr("app.services.preferences.apply_patch", stub.apply_patch)
    return stub


@pytest.fixture
def github_api() -> Iterator[respx.MockRouter]:
    """GitHub's token and user endpoints, mocked."""
    with respx.mock(assert_all_called=False) as router:
        # Named so a test can restate one leg without shadowing it: respx
        # resolves routes in insertion order, so re-adding would never win.
        router.post(github.CODE_EXCHANGE_URL, name=TOKEN_ROUTE).mock(
            return_value=httpx.Response(
                200, json={"access_token": ACCESS_TOKEN, "scope": "read:user"}
            )
        )
        router.get(github.USER_URL, name=USER_ROUTE).mock(
            return_value=httpx.Response(200, json=github_profile())
        )
        yield router


SignIn = Callable[..., httpx.Response]


@pytest.fixture
def sign_in(client: TestClient) -> SignIn:
    """Drive the full browser flow: start, then the callback GitHub would
    send the browser back to."""

    def _sign_in(code: str = OAUTH_CODE, state: str | None = None) -> httpx.Response:
        start = client.get(START_PATH, follow_redirects=False)
        assert start.status_code == 302, start.text
        callback: httpx.Response = client.get(
            CALLBACK_PATH,
            params={"code": code, "state": state or state_from_location(start.headers["location"])},
            follow_redirects=False,
        )
        return callback

    return _sign_in


@pytest.fixture
def signed_in_client(
    client: TestClient,
    github_api: respx.MockRouter,
    ensure_profile_calls: list[int],
    sign_in: SignIn,
) -> TestClient:
    """A client holding a real session and CSRF cookie pair, obtained by
    completing the OAuth flow rather than by overriding the dependency."""
    response = sign_in()
    assert response.status_code == 302, response.text
    return client


def csrf_headers(client: TestClient, settings: Settings) -> dict[str, str]:
    """The header half of the double-submit pair, as the frontend sends."""
    return {settings.csrf_header_name: client.cookies[settings.csrf_cookie_name]}


def app_with_allowed_ids(settings: Settings, engine: Engine, allowed: list[int]) -> FastAPI:
    """A second app differing only in its allow-list."""
    return create_app(settings.model_copy(update={"allowed_github_ids": allowed}), engine=engine)


# --- API tokens ---------------------------------------------------------


def bearer(value: str) -> dict[str, str]:
    """The header another application sends. No cookie goes with it."""
    return {"Authorization": f"Bearer {value}"}


IssueToken = Callable[..., str]


@pytest.fixture
def issue_token(db_session: Session, test_user: User) -> IssueToken:
    """Mint a real API token and return its raw value.

    Goes through ``app.auth.api_tokens`` rather than inserting a row, so
    the hashing, the prefix, and the display prefix are the ones the
    application would have produced — a fixture that wrote its own row
    would be testing a schema rather than a feature.
    """

    def _issue(
        user: User | None = None,
        *,
        scope: ApiTokenScope = ApiTokenScope.FULL,
        label: str = "integration",
        expires_at: datetime | None = None,
    ) -> str:
        issued = api_tokens.create_token(
            db_session, user or test_user, label=label, scope=scope, expires_at=expires_at
        )
        db_session.commit()
        return issued.value

    return _issue


@pytest.fixture
def second_user(db_session: Session) -> User:
    """A second allow-listed account, for the cross-user token tests."""
    user = User(github_id=SECOND_GITHUB_ID, github_login="hubot", display_name="Hubot")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user
