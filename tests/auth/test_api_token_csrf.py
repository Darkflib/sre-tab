"""Where CSRF and API tokens meet.

One rule, stated three ways because all three have to hold at once:

1. A request authenticated by API token is **not** challenged. It carries
   no cookie, so there is no ambient authority to forge, and demanding a
   CSRF token from an application that has no cookie jar to read one out
   of would make the feature unusable.
2. A request carrying the session cookie **is** challenged, exactly as
   before.
3. A request carrying *both* is challenged, and authenticates as the
   cookie's owner. Adding an ``Authorization`` header is not a way out of
   the check — that is the hole this file exists to keep shut.

The third is the one worth having tests for. The other two are what a
reasonable implementation does; the third is what a *plausible* one gets
wrong, by exempting on "has an ``Authorization`` header" rather than on
"has no session cookie".
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.auth.sessions import create_session
from app.db.models import ApiTokenScope, User
from app.security.csrf import generate_csrf_token
from app.settings import Settings
from tests.auth.conftest import (
    ALLOWED_GITHUB_ID,
    SECOND_GITHUB_ID,
    IssueToken,
    app_with_allowed_ids,
    bearer,
)

PREFERENCES = "/api/v1/me/preferences"
PATCH_BODY = {"theme": "dark"}


@pytest.fixture
def both_allowed(settings: Settings, engine: Engine) -> Iterator[TestClient]:
    """A client on an app whose allow-list holds both test accounts.

    Needed so that the second user's token *would* authenticate. A test
    where it could not work anyway proves nothing about precedence.
    """
    application = app_with_allowed_ids(settings, engine, [ALLOWED_GITHUB_ID, SECOND_GITHUB_ID])
    with TestClient(application, base_url="https://testserver") as test_client:
        yield test_client


def _hold_session(client: TestClient, db: Session, user: User, settings: Settings) -> str:
    """Give ``client`` a genuine session cookie and its bound CSRF token."""
    session_token = create_session(db, user, settings)
    db.commit()
    csrf_token = generate_csrf_token(settings.session_secret.get_secret_value(), session_token)
    client.cookies.set(settings.session_cookie_name, session_token)
    client.cookies.set(settings.csrf_cookie_name, csrf_token)
    return csrf_token


def test_a_bearer_mutation_needs_no_csrf_token(client: TestClient, issue_token: IssueToken) -> None:
    response = client.patch(PREFERENCES, json=PATCH_BODY, headers=bearer(issue_token()))
    assert response.status_code == 200, response.text


def test_a_cookie_mutation_still_needs_one(
    client: TestClient, db_session: Session, test_user: User, settings: Settings
) -> None:
    """The control. Without this the test above would pass just as well
    with CSRF removed from the application entirely."""
    _hold_session(client, db_session, test_user, settings)
    response = client.patch(PREFERENCES, json=PATCH_BODY)
    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF validation failed"


def test_an_authorization_header_does_not_excuse_a_cookie_request(
    client: TestClient,
    db_session: Session,
    test_user: User,
    settings: Settings,
    issue_token: IssueToken,
) -> None:
    """The attack: a browser request, plus a header, minus the CSRF token.

    A live, valid, full-scope token belonging to the very same user — so
    nothing about the credential is what refuses it. The refusal is the
    rule: the session cookie is present, therefore CSRF applies.
    """
    _hold_session(client, db_session, test_user, settings)

    response = client.patch(PREFERENCES, json=PATCH_BODY, headers=bearer(issue_token()))

    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF validation failed"


def test_both_credentials_with_a_csrf_token_authenticates_as_the_cookie(
    both_allowed: TestClient,
    db_session: Session,
    test_user: User,
    second_user: User,
    settings: Settings,
    issue_token: IssueToken,
) -> None:
    """Precedence, measured rather than asserted in a comment.

    The cookie is user A's and the token is user B's, both live and both
    allow-listed, so the answer names whichever one authenticated. It is
    A, which is what makes "CSRF applies exactly when the cookie is
    present" a complete rule rather than one with an exception in it.
    """
    csrf_token = _hold_session(both_allowed, db_session, test_user, settings)
    other_token = issue_token(second_user)

    body = both_allowed.get("/api/v1/me", headers=bearer(other_token)).json()
    assert body["user"]["id"] == test_user.id

    response = both_allowed.patch(
        PREFERENCES,
        json=PATCH_BODY,
        headers={settings.csrf_header_name: csrf_token, **bearer(other_token)},
    )
    assert response.status_code == 200, response.text


def test_the_second_users_token_works_on_its_own(
    both_allowed: TestClient, second_user: User, issue_token: IssueToken
) -> None:
    """The control for the test above: B's token is not inert, it is
    *ignored* while A's cookie is present."""
    body = both_allowed.get("/api/v1/me", headers=bearer(issue_token(second_user))).json()
    assert body["user"]["id"] == second_user.id


def test_a_read_only_token_alongside_a_cookie_does_not_narrow_the_session(
    client: TestClient,
    db_session: Session,
    test_user: User,
    settings: Settings,
    issue_token: IssueToken,
) -> None:
    """The mirror of the precedence rule, and the reason it is safe.

    If the header were consulted, a read-only token would be a way to
    downgrade — or, with the rule inverted, to upgrade — a browser
    session. It is not consulted at all: the request is the cookie's, and
    the cookie's session may write.
    """
    csrf_token = _hold_session(client, db_session, test_user, settings)
    read_only = issue_token(scope=ApiTokenScope.READ)

    response = client.patch(
        PREFERENCES,
        json=PATCH_BODY,
        headers={settings.csrf_header_name: csrf_token, **bearer(read_only)},
    )

    assert response.status_code == 200, response.text
