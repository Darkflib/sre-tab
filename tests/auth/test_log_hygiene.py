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

import json
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from app.settings import Settings
from tests.auth.conftest import ACCESS_TOKEN, CALLBACK_PATH, OAUTH_CODE, START_PATH, SignIn


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
