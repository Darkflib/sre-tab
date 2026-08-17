"""Security primitives: CSRF double-submit and session-token hashing."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.security.csrf import generate_csrf_token, require_csrf, validate_csrf_token
from app.security.tokens import compare_secret, generate_session_token, hash_session_token
from app.settings import Settings

SECRET = "test-session-secret"
SESSION = "a-session-token"
OTHER_SESSION = "another-session-token"


def test_csrf_token_roundtrip() -> None:
    token = generate_csrf_token(SECRET, SESSION)
    assert validate_csrf_token(token, SECRET, SESSION)


def test_csrf_token_tamper_and_wrong_secret_fail() -> None:
    token = generate_csrf_token(SECRET, SESSION)
    payload, _, signature = token.partition(".")
    assert not validate_csrf_token(f"{payload}x.{signature}", SECRET, SESSION)
    assert not validate_csrf_token(token, "other-secret", SESSION)
    assert not validate_csrf_token("garbage", SECRET, SESSION)


def test_csrf_token_is_bound_to_one_session() -> None:
    """The signature proves we minted it; the binding proves who for.

    Without the binding a token minted with no session in existence at
    all verified against any session, which is the whole defect: the
    cookie is script-readable by necessity, so anything able to write it
    could supply both halves of the double submit.
    """
    token = generate_csrf_token(SECRET, SESSION)
    assert not validate_csrf_token(token, SECRET, OTHER_SESSION)
    assert not validate_csrf_token(token, SECRET, "")


def test_csrf_tokens_for_two_sessions_do_not_interchange() -> None:
    mine = generate_csrf_token(SECRET, SESSION)
    theirs = generate_csrf_token(SECRET, OTHER_SESSION)
    assert validate_csrf_token(mine, SECRET, SESSION)
    assert validate_csrf_token(theirs, SECRET, OTHER_SESSION)
    assert not validate_csrf_token(theirs, SECRET, SESSION)
    assert not validate_csrf_token(mine, SECRET, OTHER_SESSION)


def test_non_ascii_csrf_material_is_false_not_an_exception() -> None:
    """``hmac.compare_digest`` raises ``TypeError`` on non-ASCII ``str``;
    uvicorn's latin-1 header decoding delivers exactly that."""
    assert not validate_csrf_token("abc\xe9.def", SECRET, SESSION)
    assert not validate_csrf_token("abc.\xe9", SECRET, SESSION)
    assert not validate_csrf_token("x.y", SECRET, "sess\xe9on")


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


def _sign_in(client: TestClient, settings: Settings, session_token: str) -> str:
    client.cookies.set(settings.session_cookie_name, session_token)
    token = generate_csrf_token(settings.session_secret.get_secret_value(), session_token)
    client.cookies.set(settings.csrf_cookie_name, token)
    return token


def test_require_csrf_rejects_missing_and_mismatched(
    csrf_client: TestClient, settings: Settings
) -> None:
    assert csrf_client.post("/mutate").status_code == 403

    _sign_in(csrf_client, settings, SESSION)
    other = generate_csrf_token(settings.session_secret.get_secret_value(), SESSION)
    response = csrf_client.post("/mutate", headers={settings.csrf_header_name: other})
    assert response.status_code == 403


def test_require_csrf_accepts_valid_pair(csrf_client: TestClient, settings: Settings) -> None:
    token = _sign_in(csrf_client, settings, SESSION)
    response = csrf_client.post("/mutate", headers={settings.csrf_header_name: token})
    assert response.status_code == 200


def test_require_csrf_refuses_a_token_minted_for_another_session(
    csrf_client: TestClient, settings: Settings
) -> None:
    """The reproduced attack: a validly signed token paired with a
    session it was never issued for."""
    foreign = generate_csrf_token(settings.session_secret.get_secret_value(), OTHER_SESSION)
    csrf_client.cookies.set(settings.session_cookie_name, SESSION)
    csrf_client.cookies.set(settings.csrf_cookie_name, foreign)
    response = csrf_client.post("/mutate", headers={settings.csrf_header_name: foreign})
    assert response.status_code == 403


def test_require_csrf_refuses_a_matching_pair_with_no_session(
    csrf_client: TestClient, settings: Settings
) -> None:
    """Nothing to bind to means nothing to verify against."""
    token = generate_csrf_token(settings.session_secret.get_secret_value(), SESSION)
    csrf_client.cookies.set(settings.csrf_cookie_name, token)
    response = csrf_client.post("/mutate", headers={settings.csrf_header_name: token})
    assert response.status_code == 403


def test_compare_secret_is_total_over_non_ascii() -> None:
    assert compare_secret("caf\xe9", "caf\xe9")
    assert not compare_secret("caf\xe9", "cafe")
    assert not compare_secret("\xe9", "")
    assert compare_secret("", "")


def test_session_tokens_hash_deterministically_and_never_match_raw() -> None:
    token = generate_session_token()
    digest = hash_session_token(token)
    assert digest == hash_session_token(token)
    assert len(digest) == 64
    assert token not in digest
    assert generate_session_token() != token
