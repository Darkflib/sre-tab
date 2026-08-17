"""Security primitives: CSRF double-submit and session-token hashing."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.security.csrf import generate_csrf_token, require_csrf, validate_csrf_token
from app.security.tokens import generate_session_token, hash_session_token
from app.settings import Settings

SECRET = "test-session-secret"


def test_csrf_token_roundtrip() -> None:
    token = generate_csrf_token(SECRET)
    assert validate_csrf_token(token, SECRET)


def test_csrf_token_tamper_and_wrong_secret_fail() -> None:
    token = generate_csrf_token(SECRET)
    payload, _, signature = token.partition(".")
    assert not validate_csrf_token(f"{payload}x.{signature}", SECRET)
    assert not validate_csrf_token(token, "other-secret")
    assert not validate_csrf_token("garbage", SECRET)


@pytest.fixture
def csrf_client(settings: Settings) -> Iterator[TestClient]:
    """Minimal app exercising the require_csrf dependency in isolation."""
    scratch = FastAPI()
    scratch.state.settings = settings

    @scratch.post("/mutate", dependencies=[Depends(require_csrf)])
    def mutate() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(scratch) as client:
        yield client


def test_require_csrf_rejects_missing_and_mismatched(
    csrf_client: TestClient, settings: Settings
) -> None:
    assert csrf_client.post("/mutate").status_code == 403

    token = generate_csrf_token(settings.session_secret.get_secret_value())
    other = generate_csrf_token(settings.session_secret.get_secret_value())
    csrf_client.cookies.set(settings.csrf_cookie_name, token)
    response = csrf_client.post("/mutate", headers={settings.csrf_header_name: other})
    assert response.status_code == 403


def test_require_csrf_accepts_valid_pair(csrf_client: TestClient, settings: Settings) -> None:
    token = generate_csrf_token(settings.session_secret.get_secret_value())
    csrf_client.cookies.set(settings.csrf_cookie_name, token)
    response = csrf_client.post("/mutate", headers={settings.csrf_header_name: token})
    assert response.status_code == 200


def test_session_tokens_hash_deterministically_and_never_match_raw() -> None:
    token = generate_session_token()
    digest = hash_session_token(token)
    assert digest == hash_session_token(token)
    assert len(digest) == 64
    assert token not in digest
    assert generate_session_token() != token
