"""The API token as a credential: its shape, and what presenting it does.

Nothing here uses ``app.dependency_overrides``. Every request carries a
real ``Authorization`` header against a real row, because the property
under test is what the middleware and ``get_current_user`` do with one —
a suite that stubbed the dependency would be asserting that FastAPI can
inject a fixture.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import api_tokens
from app.auth.csrf_middleware import UNSAFE_METHODS
from app.db.models import ApiToken, ApiTokenScope, User
from app.security.tokens import (
    API_TOKEN_DISPLAY_CHARS,
    API_TOKEN_PREFIX,
    api_token_display_prefix,
    generate_api_token,
)
from app.settings import Settings
from tests.auth.conftest import ALLOWED_GITHUB_ID, IssueToken, app_with_allowed_ids, bearer

#: One concrete request per mutating operation in the API, keyed by the
#: (method, path template) the router declares. Kept as a table so the
#: read-only refusal can be asserted against every one of them, and
#: cross-checked against the live router by
#: ``test_the_mutation_table_names_every_mutating_route`` — without that
#: check the table would silently stop covering a route added later,
#: which is the failure mode this whole file exists to prevent.
MUTATIONS: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {
    ("POST", "/api/v1/auth/logout"): ("/api/v1/auth/logout", {}),
    ("DELETE", "/api/v1/me"): ("/api/v1/me", {}),
    ("PATCH", "/api/v1/me/preferences"): ("/api/v1/me/preferences", {"json": {"theme": "dark"}}),
    ("POST", "/api/v1/me/tokens"): (
        "/api/v1/me/tokens",
        {"json": {"label": "another", "scope": "read"}},
    ),
    ("DELETE", "/api/v1/me/tokens/{token_id}"): ("/api/v1/me/tokens/1", {}),
    ("PUT", "/api/v1/items/{item_id}/read-state"): (
        "/api/v1/items/1/read-state",
        {"json": {"read": True}},
    ),
    ("PUT", "/api/v1/items/{item_id}/bookmark"): ("/api/v1/items/1/bookmark", {}),
    ("DELETE", "/api/v1/items/{item_id}/bookmark"): ("/api/v1/items/1/bookmark", {}),
}

READ_ROUTES = ["/api/v1/me", "/api/v1/sources", "/api/v1/feed", "/api/v1/bookmarks"]


def _row(db: Session, value: str) -> ApiToken:
    digest = hashlib.sha256(value.encode()).hexdigest()
    return db.execute(select(ApiToken).where(ApiToken.token_hash == digest)).scalar_one()


# --- the token itself ---------------------------------------------------


def test_a_token_is_the_fixed_prefix_and_256_bits() -> None:
    """The entropy claim in ``app.security.tokens``, measured.

    43 base64url characters is what 32 random bytes renders as, and 32
    bytes is the 256 bits the module docstring claims. Asserting the
    length is the only cheap way to notice ``_TOKEN_BYTES`` being edited
    down; asserting distinctness is the only cheap way to notice the
    randomness being removed altogether.
    """
    value = generate_api_token()
    assert value.startswith(API_TOKEN_PREFIX)
    assert len(value) == len(API_TOKEN_PREFIX) + 43
    assert len({generate_api_token() for _ in range(100)}) == 100


def test_the_display_prefix_is_a_slice_of_the_token_and_nothing_more() -> None:
    value = generate_api_token()
    prefix = api_token_display_prefix(value)
    assert value.startswith(prefix)
    assert len(prefix) == len(API_TOKEN_PREFIX) + API_TOKEN_DISPLAY_CHARS
    # It is a label, not a credential: what it withholds is the point.
    assert len(prefix) < len(value)


def test_only_a_digest_reaches_the_database(db_session: Session, issue_token: IssueToken) -> None:
    """The stored value is the hash, and the token is nowhere on the row.

    The second half is the one worth having. Checking only that
    ``token_hash`` is a digest would pass just as happily with the raw
    value copied into ``label`` — so every string column is checked, and
    a column added later is covered without editing this test.
    """
    value = issue_token(label="deployment box")
    row = _row(db_session, value)

    assert row.token_hash == hashlib.sha256(value.encode()).hexdigest()
    assert row.token_hash != value

    stored = [
        getattr(row, column.key)
        for column in ApiToken.__table__.columns
        if isinstance(getattr(row, column.key), str)
    ]
    assert stored, "no string columns were inspected — the check would be vacuous"
    assert not any(value in held for held in stored)
    # The display prefix is on the row on purpose, and is not the token.
    assert row.display_prefix in value


# --- authentication -----------------------------------------------------


@pytest.mark.parametrize("path", READ_ROUTES)
def test_a_bearer_token_authenticates_a_read_route(
    client: TestClient, issue_token: IssueToken, path: str
) -> None:
    response = client.get(path, headers=bearer(issue_token()))
    assert response.status_code == 200, response.text


def test_the_token_authenticates_as_its_owner(
    client: TestClient, test_user: User, issue_token: IssueToken
) -> None:
    body = client.get("/api/v1/me", headers=bearer(issue_token())).json()
    assert body["user"]["id"] == test_user.id
    assert body["user"]["github_id"] == test_user.github_id


@pytest.mark.parametrize("path", READ_ROUTES)
def test_a_read_only_token_reaches_read_routes(
    client: TestClient, issue_token: IssueToken, path: str
) -> None:
    """The refusals below would pass with the scope wired to reject
    everything; this is what says it does not."""
    token = issue_token(scope=ApiTokenScope.READ)
    assert client.get(path, headers=bearer(token)).status_code == 200


def test_a_full_token_may_mutate_without_a_csrf_header(
    client: TestClient, issue_token: IssueToken
) -> None:
    response = client.patch(
        "/api/v1/me/preferences", json={"theme": "dark"}, headers=bearer(issue_token())
    )
    assert response.status_code == 200, response.text
    assert response.json()["theme"] == "dark"


# --- scope --------------------------------------------------------------


def test_the_mutation_table_names_every_mutating_route(app: FastAPI) -> None:
    """The table above is complete, asked of the application rather than
    of me.

    Without this the read-only refusal below is only ever as good as the
    day the table was written: a mutating route added afterwards would
    not be in it, every parametrised case would go on passing, and the
    suite would report that read-only tokens cannot write while never
    having tried the new route.

    Read out of the published schema rather than by walking
    ``app.routes``, which is not the flat list it looks like — FastAPI
    nests included routers, so the obvious traversal finds no ``APIRoute``
    at all and the comparison passes vacuously against an empty set. That
    is precisely the silent-pass shape this check exists to avoid, and it
    is how the first draft of it behaved.
    """
    declared = {
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        for method in item
        if method.upper() in UNSAFE_METHODS
    }
    assert declared, "no mutating operations were found — the comparison would be vacuous"
    assert declared == set(MUTATIONS)


@pytest.mark.parametrize(("method", "template"), sorted(MUTATIONS))
def test_a_read_only_token_is_refused_on_every_mutating_route(
    client: TestClient, issue_token: IssueToken, method: str, template: str
) -> None:
    path, kwargs = MUTATIONS[(method, template)]
    token = issue_token(scope=ApiTokenScope.READ)

    response = client.request(method, path, headers=bearer(token), **kwargs)

    assert response.status_code == 403, f"{method} {path}: {response.text}"
    assert "read-only" in response.json()["detail"]


def test_the_scope_refusal_happens_before_the_route_runs(
    client: TestClient, db_session: Session, test_user: User, issue_token: IssueToken
) -> None:
    """A read-only token cannot delete the account, and the reason is not
    that ``delete_me`` checked: it never ran."""
    token = issue_token(scope=ApiTokenScope.READ)

    assert client.delete("/api/v1/me", headers=bearer(token)).status_code == 403

    assert db_session.get(User, test_user.id) is not None


# --- refusals, all alike ------------------------------------------------


def test_unknown_revoked_expired_and_malformed_are_refused_identically(
    client: TestClient, db_session: Session, test_user: User, issue_token: IssueToken
) -> None:
    """Five ways to fail, one answer.

    Compared against each other *and* against the no-credential case, so
    a caller cannot tell a revoked token from a token that never existed
    — and cannot tell either from not having presented one.
    """
    revoked = issue_token()
    api_tokens.revoke_token(db_session, test_user, _row(db_session, revoked).id)
    db_session.commit()

    expired = issue_token(expires_at=datetime.now(UTC) - timedelta(seconds=1))

    presented = {
        "unknown": f"{API_TOKEN_PREFIX}QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE",
        "revoked": revoked,
        "expired": expired,
        "malformed": "not-a-token-at-all",
        "bare-prefix": API_TOKEN_PREFIX,
    }
    answers = {
        name: client.get("/api/v1/me", headers=bearer(value)) for name, value in presented.items()
    }
    baseline = client.get("/api/v1/me")

    for name, response in answers.items():
        assert response.status_code == 401, name
        assert response.json() == baseline.json() == {"detail": "Not signed in"}, name


def test_a_valid_token_still_works(client: TestClient, issue_token: IssueToken) -> None:
    """The control for the test above, which would pass with every token
    refused."""
    assert client.get("/api/v1/me", headers=bearer(issue_token())).status_code == 200


@pytest.mark.parametrize(
    "header",
    ["", "Basic abc", "Bearer", "Bearer ", "Token sretab_pat_x", "bearer"],
    ids=["empty", "basic", "no-space", "no-credential", "wrong-scheme", "scheme-only"],
)
def test_an_authorization_header_that_is_not_a_bearer_credential_is_ignored(
    client: TestClient, header: str
) -> None:
    response = client.get("/api/v1/me", headers={"Authorization": header})
    assert response.status_code == 401


def test_the_bearer_scheme_is_matched_case_insensitively(
    client: TestClient, issue_token: IssueToken
) -> None:
    """RFC 9110 says the scheme is case-insensitive, and clients differ."""
    token = issue_token()
    for scheme in ("Bearer", "bearer", "BEARER"):
        response = client.get("/api/v1/me", headers={"Authorization": f"{scheme} {token}"})
        assert response.status_code == 200, scheme


# --- authorisation ------------------------------------------------------


def test_removal_from_the_allow_list_kills_a_live_token(
    settings: Settings, engine: Any, db_session: Session, issue_token: IssueToken
) -> None:
    """The whole point of re-checking rather than trusting sign-in.

    The row is untouched and still live by every column on it; what
    changed is the operator's allow-list, and the token stops working
    because that is now consulted on the request rather than only once,
    months ago, when the session behind it was created.
    """
    token = issue_token()

    with TestClient(app_with_allowed_ids(settings, engine, [ALLOWED_GITHUB_ID])) as allowed:
        assert allowed.get("/api/v1/me", headers=bearer(token)).status_code == 200

    with TestClient(app_with_allowed_ids(settings, engine, [])) as removed:
        response = removed.get("/api/v1/me", headers=bearer(token))

    assert response.status_code == 401
    assert response.json() == {"detail": "Not signed in"}
    # Still live in the database: nothing revoked it, and nothing should.
    row = _row(db_session, token)
    assert row.revoked_at is None
    assert row.expires_at is None


def test_an_allow_list_holding_somebody_else_does_not_admit_this_token(
    settings: Settings, engine: Any, issue_token: IssueToken
) -> None:
    """An empty allow-list denies everyone, so a test using only that one
    would pass against ``is_authorised`` returning False unconditionally."""
    token = issue_token()
    with TestClient(app_with_allowed_ids(settings, engine, [999999])) as other:
        assert other.get("/api/v1/me", headers=bearer(token)).status_code == 401


def test_deleting_the_account_takes_its_tokens_with_it(
    client: TestClient, db_session: Session, issue_token: IssueToken, settings: Settings
) -> None:
    """``DELETE /me`` is one statement leaning on ``ondelete="CASCADE"``;
    this is the half of that claim that is new."""
    token = issue_token()
    assert client.delete("/api/v1/me", headers=bearer(token)).status_code == 204

    db_session.expire_all()
    assert db_session.execute(select(ApiToken)).scalars().all() == []
    assert client.get("/api/v1/me", headers=bearer(token)).status_code == 401


# --- last_used_at -------------------------------------------------------


def test_last_used_at_is_null_until_the_token_is_presented(
    db_session: Session, issue_token: IssueToken
) -> None:
    assert _row(db_session, issue_token()).last_used_at is None


def test_last_used_at_moves_on_a_successful_request(
    client: TestClient, db_session: Session, issue_token: IssueToken
) -> None:
    token = issue_token()
    before = datetime.now(UTC)

    assert client.get("/api/v1/me", headers=bearer(token)).status_code == 200

    db_session.expire_all()
    first = _row(db_session, token).last_used_at
    assert first is not None
    assert first.replace(tzinfo=UTC) >= before.replace(microsecond=0)

    client.get("/api/v1/sources", headers=bearer(token))
    db_session.expire_all()
    second = _row(db_session, token).last_used_at
    assert second is not None
    assert second >= first


def test_a_refused_request_does_not_move_last_used_at(
    client: TestClient, db_session: Session, test_user: User, issue_token: IssueToken
) -> None:
    """Revoked, so it never authenticated. The timestamp says "used", and
    a token that was refused was not used."""
    token = issue_token()
    api_tokens.revoke_token(db_session, test_user, _row(db_session, token).id)
    db_session.commit()

    assert client.get("/api/v1/me", headers=bearer(token)).status_code == 401

    db_session.expire_all()
    assert _row(db_session, token).last_used_at is None


def test_a_scope_refusal_still_counts_as_a_use(
    client: TestClient, db_session: Session, issue_token: IssueToken
) -> None:
    """It authenticated and was then refused on what it asked to do. The
    owner should be able to see that it is in service."""
    token = issue_token(scope=ApiTokenScope.READ)

    assert client.delete("/api/v1/me", headers=bearer(token)).status_code == 403

    db_session.expire_all()
    assert _row(db_session, token).last_used_at is not None
