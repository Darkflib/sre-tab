"""``/api/v1/me/tokens`` — what the Settings screen drives.

Every request here goes through a real session cookie rather than a
dependency override, because two of the properties under test are about
credentials: that the raw token is returned once and never again, and
that these routes refuse an API token even when it would otherwise
authenticate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.sessions import create_session
from app.db.models import ApiTokenScope, User
from app.security.csrf import generate_csrf_token
from app.security.tokens import API_TOKEN_PREFIX
from app.settings import Settings
from tests.auth.conftest import IssueToken, bearer

TOKENS = "/api/v1/me/tokens"


class Signed:
    """A client holding a genuine session, and the CSRF header for it."""

    def __init__(self, client: TestClient, headers: dict[str, str]) -> None:
        self.client = client
        self.headers = headers

    def get(self, path: str = TOKENS) -> dict[str, Any]:
        response = self.client.get(path)
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    def create(self, **body: object) -> httpx.Response:
        payload = {"label": "laptop", "scope": "full", **body}
        response: httpx.Response = self.client.post(TOKENS, json=payload, headers=self.headers)
        return response

    def revoke(self, token_id: int) -> httpx.Response:
        response: httpx.Response = self.client.delete(f"{TOKENS}/{token_id}", headers=self.headers)
        return response


def anonymous(app: FastAPI) -> TestClient:
    """A client with no credentials at all, for presenting a token to."""
    return TestClient(app, base_url="https://testserver")


def _sign(client: TestClient, db: Session, user: User, settings: Settings) -> Signed:
    """Give ``client`` a genuine session for ``user``.

    Each caller passes its *own* client. Two users signed into one client
    would only ever be one session — the second ``set`` overwrites the
    first — so an isolation test written that way would be asking one
    session two questions and reading the answer as two users agreeing.
    """
    session_token = create_session(db, user, settings)
    db.commit()
    csrf_token = generate_csrf_token(settings.session_secret.get_secret_value(), session_token)
    client.cookies.set(settings.session_cookie_name, session_token)
    client.cookies.set(settings.csrf_cookie_name, csrf_token)
    return Signed(client, {settings.csrf_header_name: csrf_token})


@pytest.fixture
def signed(client: TestClient, db_session: Session, test_user: User, settings: Settings) -> Signed:
    return _sign(client, db_session, test_user, settings)


# --- creation -----------------------------------------------------------


def test_creation_returns_the_raw_token_once(signed: Signed) -> None:
    response = signed.create(label="deployment box", scope="read")
    assert response.status_code == 201, response.text

    body = response.json()
    value = body["value"]
    assert value.startswith(API_TOKEN_PREFIX)
    assert body["token"]["label"] == "deployment box"
    assert body["token"]["scope"] == "read"
    assert body["token"]["display_prefix"] == value[: len(body["token"]["display_prefix"])]
    assert body["token"]["last_used_at"] is None
    assert body["token"]["expires_at"] is None


def test_the_token_is_never_returned_again(signed: Signed) -> None:
    """The claim the feature rests on, checked against the whole document
    rather than against the fields I remembered to name."""
    value = signed.create().json()["value"]

    listing = signed.client.get(TOKENS)

    assert value not in listing.text
    assert all("value" not in entry for entry in listing.json()["tokens"])
    # And it is genuinely a live token, not an empty listing being asked
    # whether it contains something.
    assert listing.json()["tokens"], listing.text
    assert signed.client.get("/api/v1/me", headers=bearer(value)).status_code == 200


def test_a_created_token_authenticates(signed: Signed, app: FastAPI) -> None:
    value = signed.create(scope="read").json()["value"]
    fresh = anonymous(app)
    assert fresh.get("/api/v1/me", headers=bearer(value)).status_code == 200


def test_an_expiry_in_days_becomes_a_timestamp(signed: Signed) -> None:
    body = signed.create(expires_in_days=30).json()
    expires_at = datetime.fromisoformat(body["token"]["expires_at"])
    expected = datetime.now(UTC) + timedelta(days=30)
    assert abs((expires_at - expected).total_seconds()) < 60


@pytest.mark.parametrize(
    "body",
    [
        {"scope": "full"},
        {"label": "x"},
        {"label": "", "scope": "full"},
        {"label": "   ", "scope": "full"},
        {"label": "x" * 101, "scope": "full"},
        {"label": "x", "scope": "admin"},
        {"label": "x", "scope": "full", "expires_in_days": 0},
        {"label": "x", "scope": "full", "expires_in_days": 100000},
    ],
    ids=[
        "no-label",
        "no-scope",
        "empty-label",
        "whitespace-label",
        "over-long-label",
        "unknown-scope",
        "zero-days",
        "absurd-expiry",
    ],
)
def test_a_bad_creation_request_is_refused(signed: Signed, body: dict[str, Any]) -> None:
    response = signed.client.post(TOKENS, json=body, headers=signed.headers)
    assert response.status_code == 422, response.text


def test_the_scope_has_no_default(signed: Signed) -> None:
    """Two scopes that differ by their whole blast radius: the choice is
    made, not inherited. A client omitting it gets a 422, not the
    convenient one."""
    assert (
        signed.client.post(TOKENS, json={"label": "x"}, headers=signed.headers).status_code == 422
    )


# --- listing ------------------------------------------------------------


def test_listing_is_newest_first_and_describes_each_token(signed: Signed) -> None:
    signed.create(label="older", scope="read")
    signed.create(label="newer", scope="full")

    tokens = signed.get()["tokens"]

    assert [entry["label"] for entry in tokens] == ["newer", "older"]
    assert [entry["scope"] for entry in tokens] == ["full", "read"]
    assert all(entry["display_prefix"].startswith(API_TOKEN_PREFIX) for entry in tokens)


def test_last_used_at_shows_in_the_listing(signed: Signed, app: FastAPI) -> None:
    """The stale-token affordance: the row says when it was last used, so
    a forgotten one is visible rather than merely present."""
    value = signed.create().json()["value"]
    assert signed.get()["tokens"][0]["last_used_at"] is None

    fresh = anonymous(app)
    fresh.get("/api/v1/sources", headers=bearer(value))

    assert signed.get()["tokens"][0]["last_used_at"] is not None


# --- revocation ---------------------------------------------------------


def test_revoking_a_token_stops_it_working(signed: Signed, app: FastAPI) -> None:
    created = signed.create().json()
    value = created["value"]
    fresh = anonymous(app)
    assert fresh.get("/api/v1/me", headers=bearer(value)).status_code == 200

    assert signed.revoke(created["token"]["id"]).status_code == 204

    assert fresh.get("/api/v1/me", headers=bearer(value)).status_code == 401
    assert signed.get()["tokens"] == []


def test_revoking_an_unknown_id_is_a_no_op(signed: Signed) -> None:
    """204 rather than 404, on the reasoning ``tests/api/test_isolation.py``
    pins for bookmarks: a different answer would confirm a guessed id."""
    assert signed.revoke(999_999).status_code == 204


def test_revoking_twice_is_a_no_op(signed: Signed) -> None:
    token_id = signed.create().json()["token"]["id"]
    assert signed.revoke(token_id).status_code == 204
    assert signed.revoke(token_id).status_code == 204


# --- one user cannot reach another's ------------------------------------


def test_a_second_user_sees_none_of_the_first_users_tokens(
    app: FastAPI,
    client: TestClient,
    db_session: Session,
    test_user: User,
    second_user: User,
    settings: Settings,
) -> None:
    mine = _sign(client, db_session, test_user, settings)
    mine.create(label="mine")

    theirs = _sign(anonymous(app), db_session, second_user, settings)
    theirs.create(label="theirs")

    assert [entry["label"] for entry in theirs.get()["tokens"]] == ["theirs"]
    assert [entry["label"] for entry in mine.get()["tokens"]] == ["mine"]


def test_a_second_user_cannot_revoke_the_first_users_token(
    app: FastAPI,
    client: TestClient,
    db_session: Session,
    test_user: User,
    second_user: User,
    settings: Settings,
) -> None:
    """B guessing A's token id gets its own no-op 204, and A's token goes
    on working — the same shape the bookmark isolation suite asserts."""
    mine = _sign(client, db_session, test_user, settings)
    created = mine.create().json()
    value = created["value"]

    theirs = _sign(anonymous(app), db_session, second_user, settings)
    assert theirs.revoke(created["token"]["id"]).status_code == 204

    assert anonymous(app).get("/api/v1/me", headers=bearer(value)).status_code == 200
    assert len(mine.get()["tokens"]) == 1


# --- these routes refuse an API token -----------------------------------


def test_a_full_token_cannot_list_tokens(client: TestClient, issue_token: IssueToken) -> None:
    """Revocation has to mean something. A ``FULL`` token that could mint
    a replacement would make revoking a leaked one a gesture."""
    response = client.get(TOKENS, headers=bearer(issue_token()))
    assert response.status_code == 403
    assert "signed-in session" in response.json()["detail"]


def test_a_full_token_cannot_mint_another(client: TestClient, issue_token: IssueToken) -> None:
    response = client.post(
        TOKENS, json={"label": "escalation", "scope": "full"}, headers=bearer(issue_token())
    )
    assert response.status_code == 403
    assert "signed-in session" in response.json()["detail"]


def test_a_full_token_cannot_revoke_a_token(
    app: FastAPI, client: TestClient, db_session: Session, test_user: User, settings: Settings
) -> None:
    signed = _sign(client, db_session, test_user, settings)
    token_id = signed.create().json()["token"]["id"]
    other = signed.create(label="second").json()["value"]

    fresh = anonymous(app)
    response = fresh.delete(f"{TOKENS}/{token_id}", headers=bearer(other))

    assert response.status_code == 403
    assert fresh.get("/api/v1/me", headers=bearer(other)).status_code == 200


def test_a_read_only_token_is_refused_on_scope_first(
    client: TestClient, issue_token: IssueToken
) -> None:
    """Two rules apply to this request and the scope one wins, because it
    is decided in middleware before the route's dependencies run."""
    response = client.post(
        TOKENS,
        json={"label": "x", "scope": "read"},
        headers=bearer(issue_token(scope=ApiTokenScope.READ)),
    )
    assert response.status_code == 403
    assert "read-only" in response.json()["detail"]


def test_an_unauthenticated_request_is_401(client: TestClient) -> None:
    assert client.get(TOKENS).status_code == 401
