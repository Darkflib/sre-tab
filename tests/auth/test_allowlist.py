"""Allow-list authorisation — the fail-closed property, unit and end to end."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.auth.allowlist import is_authorised
from app.db.models import User
from app.settings import Settings
from tests.auth.conftest import (
    ALLOWED_GITHUB_ID,
    CALLBACK_PATH,
    DENIED_GITHUB_ID,
    START_PATH,
    USER_ROUTE,
    app_with_allowed_ids,
    github_profile,
    state_from_location,
)


def _settings_with(settings: Settings, allowed: list[int]) -> Settings:
    return settings.model_copy(update={"allowed_github_ids": allowed})


def test_empty_allow_list_denies_everyone(settings: Settings) -> None:
    """An unconfigured instance is one nobody may sign in to."""
    empty = _settings_with(settings, [])
    for github_id in (ALLOWED_GITHUB_ID, DENIED_GITHUB_ID, 0, -1):
        assert is_authorised(github_id, empty) is False


def test_populated_allow_list_admits_only_listed_ids(settings: Settings) -> None:
    populated = _settings_with(settings, [ALLOWED_GITHUB_ID])
    assert is_authorised(ALLOWED_GITHUB_ID, populated) is True
    assert is_authorised(DENIED_GITHUB_ID, populated) is False


@pytest.mark.parametrize(
    ("allowed", "profile_id"),
    [
        pytest.param([], ALLOWED_GITHUB_ID, id="empty-list-denies-a-valid-callback"),
        pytest.param([ALLOWED_GITHUB_ID], DENIED_GITHUB_ID, id="unlisted-id-denied"),
    ],
)
def test_denied_callback_creates_no_user(
    settings: Settings,
    engine: Engine,
    db_session: Session,
    github_api: respx.MockRouter,
    ensure_profile_calls: list[int],
    allowed: list[int],
    profile_id: int,
) -> None:
    """The OAuth exchange itself succeeds; authorisation is what refuses.

    The row count is the point: authorisation is checked before any write,
    so a denied identity leaves no trace in the database.
    """
    github_api[USER_ROUTE].mock(
        return_value=httpx.Response(200, json=github_profile(github_id=profile_id))
    )
    app = app_with_allowed_ids(settings, engine, allowed)

    with TestClient(app, base_url="https://testserver") as client:
        start = client.get(START_PATH, follow_redirects=False)
        response = client.get(
            CALLBACK_PATH,
            params={
                "code": "valid-code",
                "state": state_from_location(start.headers["location"]),
            },
            follow_redirects=False,
        )

    assert response.status_code == 403
    assert db_session.execute(select(func.count()).select_from(User)).scalar_one() == 0
    assert ensure_profile_calls == []
