"""The callback's non-happy paths that are not attacks.

A user who clicks "Cancel" on GitHub's consent screen is redirected to
``?error=access_denied&error_description=…&state=…`` with no ``code``.
Declaring ``code`` a required query parameter turned that ordinary
outcome into FastAPI's 422 — a validation-error document rendered in the
browser where a sentence belongs.

So: no required query parameters, and the branch redirects to the landing
page with a fixed outcome token. What it must *not* do is get lax about
anything else — no session, no cookie, and the failure budget still
charged, because a grinder reaches this branch as easily as a user.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.auth import callback_failure_limiter
from app.db.models import User, UserSession
from app.settings import Settings
from tests.auth.conftest import CALLBACK_PATH


def _outcome(location: str) -> str:
    return parse_qs(urlparse(location).query)["signin"][0]


def test_a_user_denial_redirects_to_the_landing_page(
    client: TestClient, settings: Settings
) -> None:
    response = client.get(
        CALLBACK_PATH,
        params={
            "error": "access_denied",
            "error_description": "The user has denied your application access.",
            "state": "whatever-github-echoed",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(settings.app_base_url.rstrip("/") + "/")
    assert _outcome(location) == "cancelled"


def test_a_user_denial_creates_nothing_and_sets_no_cookie(
    client: TestClient, db_session: Session, settings: Settings
) -> None:
    response = client.get(
        CALLBACK_PATH,
        params={"error": "access_denied", "state": "echoed"},
        follow_redirects=False,
    )

    assert settings.session_cookie_name not in response.cookies
    assert db_session.scalar(select(func.count()).select_from(User)) == 0
    assert db_session.scalar(select(func.count()).select_from(UserSession)) == 0


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"state": "echoed"},
        {"code": "a-code-with-no-state"},
        {"error": "server_error", "state": "echoed"},
        {"error": "application_suspended"},
        # A code *and* an error is contradictory; the error wins.
        {"code": "a-code", "state": "echoed", "error": "access_denied"},
    ],
)
def test_an_incomplete_callback_redirects_rather_than_422s(
    client: TestClient, params: dict[str, str]
) -> None:
    response = client.get(CALLBACK_PATH, params=params, follow_redirects=False)
    assert response.status_code == 302


def test_only_an_explicit_denial_reads_as_cancelled(client: TestClient) -> None:
    """Everything else is "failed": the distinction the user cares about
    is "I did that" versus "something went wrong"."""
    cancelled = client.get(
        CALLBACK_PATH,
        params={"error": "access_denied", "state": "echoed"},
        follow_redirects=False,
    )
    failed = client.get(
        CALLBACK_PATH,
        params={"error": "server_error", "state": "echoed"},
        follow_redirects=False,
    )
    assert _outcome(cancelled.headers["location"]) == "cancelled"
    assert _outcome(failed.headers["location"]) == "failed"


def test_the_outcome_token_is_chosen_here_not_echoed_from_github(client: TestClient) -> None:
    """The value lands in a URL the browser follows, so an upstream error
    code must never be reflected into it."""
    response = client.get(
        CALLBACK_PATH,
        params={
            "error": '"><script>alert(1)</script>',
            "error_description": "<img src=x onerror=alert(1)>",
            "state": "echoed",
        },
        follow_redirects=False,
    )
    location = response.headers["location"]
    assert _outcome(location) == "failed"
    assert "script" not in location
    assert "onerror" not in location


def test_a_declined_callback_still_costs_failure_budget(client: TestClient) -> None:
    """Otherwise the branch would be a free, unmetered endpoint."""
    for _ in range(callback_failure_limiter.limit):
        assert (
            client.get(
                CALLBACK_PATH,
                params={"error": "access_denied", "state": "echoed"},
                follow_redirects=False,
            ).status_code
            == 302
        )

    exhausted = client.get(
        CALLBACK_PATH,
        params={"error": "access_denied", "state": "echoed"},
        follow_redirects=False,
    )
    assert exhausted.status_code == 429


def test_the_error_description_is_not_logged_verbatim(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Free text from upstream; the fixed error code is what is useful."""
    client.get(
        CALLBACK_PATH,
        params={
            "error": "access_denied",
            "error_description": "distinctive-upstream-prose-42",
            "state": "echoed",
        },
        follow_redirects=False,
    )
    written = capsys.readouterr().out
    assert "oauth_callback_declined" in written
    assert "distinctive-upstream-prose-42" not in written
