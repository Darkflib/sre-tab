"""Sign-in orchestration.

The route layer stays thin: it handles HTTP (cookies, redirects, status
codes, rate limiting) and delegates the decision-making here. Everything in
this module participates in the caller's transaction and never commits, so
a failure anywhere — including the ``NotImplementedError`` that
``ensure_profile`` still raises while agent C is in flight — leaves no
half-created account behind.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

import structlog
from fastapi import status
from sqlalchemy.orm import Session

from app.auth import allowlist
from app.auth.github import GitHubOAuthError, GitHubProfile, build_authorize_url
from app.auth.github import exchange_code as _exchange_code
from app.auth.github import fetch_profile as _fetch_profile
from app.auth.sessions import create_session, revoke_session
from app.auth.state import DEFAULT_TTL_SECONDS, state_store
from app.auth.users import upsert_user
from app.db.models import User
from app.services import preferences
from app.settings import Settings

log = structlog.get_logger(__name__)

STATE_TTL_SECONDS = DEFAULT_TTL_SECONDS


class SignInDenied(Exception):
    """Sign-in refused. ``reason`` is a fixed token safe to log; ``detail``
    is what the caller may show. Neither ever carries a credential."""

    def __init__(
        self, reason: str, detail: str, status_code: int = status.HTTP_403_FORBIDDEN
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class Authorization:
    url: str
    state: str
    ttl_seconds: int = STATE_TTL_SECONDS


def start_authorization(settings: Settings) -> Authorization:
    state = state_store.issue(settings.session_secret.get_secret_value())
    return Authorization(url=build_authorize_url(settings, state), state=state)


def _validate_state(settings: Settings, state: str, state_cookie: str | None) -> None:
    # The cookie binds the redemption to the browser that began the flow;
    # the store makes redemption single-use; the signature and embedded
    # expiry make the token unforgeable and short-lived.
    if not state_cookie or not hmac.compare_digest(state_cookie, state):
        raise SignInDenied("state_unbound", "Sign-in request could not be verified.")
    if not state_store.consume(state, settings.session_secret.get_secret_value()):
        raise SignInDenied("state_invalid", "Sign-in request expired or was already used.")


def _load_profile(settings: Settings, code: str) -> GitHubProfile:
    try:
        access_token = _exchange_code(settings, code)
        # The token's whole life is this expression: used once, never
        # returned to the caller, never stored, never logged.
        return _fetch_profile(access_token)
    except GitHubOAuthError as exc:
        log.warning("oauth_github_exchange_failed", error=str(exc))
        raise SignInDenied(
            "github_unavailable",
            "GitHub sign-in could not be completed.",
            status.HTTP_502_BAD_GATEWAY,
        ) from exc


def complete_sign_in(
    db: Session,
    settings: Settings,
    *,
    code: str,
    state: str,
    state_cookie: str | None,
    current_session_token: str | None = None,
) -> tuple[User, str]:
    """Run the callback half of the flow and return the user plus the raw
    session token. The caller commits."""
    _validate_state(settings, state, state_cookie)
    profile = _load_profile(settings, code)

    # Authorisation before persistence: a denied identity leaves no row.
    if not allowlist.is_authorised(profile.github_id, settings):
        log.warning("oauth_denied_not_allow_listed", github_id=profile.github_id)
        raise SignInDenied("not_allow_listed", "This GitHub account is not permitted to sign in.")

    user = upsert_user(db, profile)
    preferences.ensure_profile(db, user)

    # Rotate: whatever session the browser arrived with is revoked, and a
    # fresh token is issued, so a pre-session-fixation token is worthless.
    if current_session_token:
        revoke_session(db, current_session_token)
    token = create_session(db, user, settings)

    log.info("sign_in_succeeded", user_id=user.id, github_id=user.github_id)
    return user, token
