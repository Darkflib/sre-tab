"""No secret or token reaches the emitted logs.

These read the JSON structlog actually writes, rather than intercepting the
processor chain. Two reasons. It is the property that matters — what lands
in the operator's log aggregator. And the redaction processor in
``app.logging`` is a backstop, not a licence (AGENTS.md): checking the
rendered output would still catch a leak that redaction happened to miss,
whereas checking only pre-redaction events would not tell us what was
written.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.sessions import create_session
from app.db.models import ApiTokenScope, User
from app.security.csrf import generate_csrf_token
from app.settings import Settings
from tests.auth.conftest import (
    ACCESS_TOKEN,
    CALLBACK_PATH,
    OAUTH_CODE,
    START_PATH,
    IssueToken,
    SignIn,
    bearer,
)


def _emitted(capsys: pytest.CaptureFixture[str]) -> tuple[str, list[dict[str, Any]]]:
    """Everything written to stdout, plus the structlog lines parsed out."""
    written = capsys.readouterr().out
    events: list[dict[str, Any]] = []
    for line in written.splitlines():
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return written, events


def test_successful_sign_in_emits_no_credentials(
    client: TestClient,
    settings: Settings,
    github_api: respx.MockRouter,
    sign_in: SignIn,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert sign_in().status_code == 302
    session_token = client.cookies[settings.session_cookie_name]
    csrf_token = client.cookies[settings.csrf_cookie_name]

    written, events = _emitted(capsys)

    # Guard against a vacuous pass: the flow must actually have logged.
    assert any(event.get("event") == "sign_in_succeeded" for event in events), written

    for secret in (
        OAUTH_CODE,
        ACCESS_TOKEN,
        settings.github_client_secret.get_secret_value(),
        settings.session_secret.get_secret_value(),
        session_token,
        csrf_token,
    ):
        assert secret not in written


def test_denied_sign_in_emits_no_credentials(
    client: TestClient,
    settings: Settings,
    github_api: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client.get(START_PATH, follow_redirects=False)
    response = client.get(
        CALLBACK_PATH,
        params={"code": OAUTH_CODE, "state": "forged.9999999999.deadbeef"},
        follow_redirects=False,
    )
    assert response.status_code == 403

    written, events = _emitted(capsys)

    denials = [event for event in events if event.get("event") == "oauth_callback_denied"]
    assert denials, written
    # A fixed reason token, not the material that failed to validate.
    assert denials[0]["reason"] == "state_unbound"

    assert OAUTH_CODE not in written
    assert settings.github_client_secret.get_secret_value() not in written


def test_request_logging_omits_the_query_string(
    client: TestClient,
    github_api: respx.MockRouter,
    sign_in: SignIn,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The OAuth code travels in the query string, so the per-request line
    must record the path alone."""
    sign_in()

    written, events = _emitted(capsys)
    completions = [event for event in events if event.get("event") == "request_completed"]

    assert any(event["path"] == CALLBACK_PATH for event in completions), written
    assert all("?" not in str(event["path"]) for event in completions)


def test_sign_in_logs_the_identity_it_is_allowed_to_log(
    client: TestClient,
    github_api: respx.MockRouter,
    sign_in: SignIn,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The useful half of the trade: an operator can still answer "who
    signed in, and when"."""
    sign_in()

    _written, events = _emitted(capsys)
    success = next(event for event in events if event.get("event") == "sign_in_succeeded")
    assert success["github_id"] == 1000001
    assert success["user_id"]
    assert "request_id" in success


# --- API tokens ---------------------------------------------------------


def test_creating_a_token_emits_no_token(
    client: TestClient,
    db_session: Session,
    test_user: User,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The one response in the API that carries a raw credential is also
    the one place a logger would most plausibly be handed it."""
    session_token = create_session(db_session, test_user, settings)
    db_session.commit()
    csrf_token = generate_csrf_token(settings.session_secret.get_secret_value(), session_token)
    client.cookies.set(settings.session_cookie_name, session_token)
    client.cookies.set(settings.csrf_cookie_name, csrf_token)

    response = client.post(
        "/api/v1/me/tokens",
        json={"label": "deployment box", "scope": "full"},
        headers={settings.csrf_header_name: csrf_token},
    )
    assert response.status_code == 201, response.text
    value = response.json()["value"]

    written, events = _emitted(capsys)

    # Guard against a vacuous pass: the route must actually have logged.
    created = [event for event in events if event.get("event") == "api_token_created"]
    assert created, written
    assert value not in written

    # The digest is derived from the token and is equally out of bounds —
    # a leak of it would let an attacker recognise a token they held.
    assert hashlib.sha256(value.encode()).hexdigest() not in written
    # What an operator *can* have: who, which row, and what it may do.
    assert created[0]["user_id"] == test_user.id
    assert created[0]["scope"] == "full"


def test_using_a_token_emits_no_token(
    client: TestClient, issue_token: IssueToken, capsys: pytest.CaptureFixture[str]
) -> None:
    value = issue_token(label="ci runner")
    assert client.get("/api/v1/me", headers=bearer(value)).status_code == 200

    written, events = _emitted(capsys)

    assert any(event.get("event") == "request_completed" for event in events), written
    assert value not in written
    assert hashlib.sha256(value.encode()).hexdigest() not in written


def test_a_refused_token_emits_no_token(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure path is the one that would be tempting to log verbatim
    — "what did they actually send?" — and it is the one where the value
    is most likely to be somebody's real token, mistyped."""
    presented = "sretab_pat_thisisnotarealtokenbutmustnotbeloggedanyway1"
    assert client.get("/api/v1/me", headers=bearer(presented)).status_code == 401

    written, events = _emitted(capsys)

    assert any(event.get("event") == "request_completed" for event in events), written
    assert presented not in written


def test_a_scope_refusal_emits_no_token(
    client: TestClient, issue_token: IssueToken, capsys: pytest.CaptureFixture[str]
) -> None:
    value = issue_token(scope=ApiTokenScope.READ)
    assert client.delete("/api/v1/me", headers=bearer(value)).status_code == 403

    written, events = _emitted(capsys)

    refusals = [event for event in events if event.get("event") == "api_token_scope_refused"]
    assert refusals, written
    assert refusals[0]["method"] == "DELETE"
    assert value not in written
