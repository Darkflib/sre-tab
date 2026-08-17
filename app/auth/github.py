"""GitHub OAuth 2.0 authorization-code client — server-side only.

Two secrets pass through this module and neither may escape it: the client
secret, which is sent to GitHub and nowhere else, and the access token,
which is used once to read the profile and then discarded. Neither is
returned to a caller that could serialise it into a response, persisted, or
passed to the logger. Callers receive a :class:`GitHubProfile` and nothing
more (see :func:`fetch_profile`); the token's lifetime is a single function
call in :mod:`app.auth.flow`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.settings import Settings

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
CODE_EXCHANGE_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"

# The allow-list keys off the numeric ID, so the public profile is all we
# need; no email, no organisation, no repository scopes.
OAUTH_SCOPE = "read:user"

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_MAX_RESPONSE_BYTES = 256 * 1024


class GitHubOAuthError(RuntimeError):
    """GitHub refused the exchange, or returned an unusable profile."""


@dataclass(frozen=True)
class GitHubProfile:
    """The mutable profile fields v1 stores, plus the identity anchor."""

    github_id: int
    login: str
    display_name: str | None
    avatar_url: str | None


def build_authorize_url(settings: Settings, state: str) -> str:
    """The URL the browser is redirected to.

    ``redirect_uri`` is ``settings.github_redirect_uri`` verbatim — the same
    value sent at code exchange, and the value registered on the OAuth app,
    so GitHub's exact-match check is the outer guard against redirection to
    anywhere else.
    """
    params = {
        "client_id": settings.github_client_id,
        "redirect_uri": settings.github_redirect_uri,
        "scope": OAUTH_SCOPE,
        "state": state,
        "allow_signup": "false",
    }
    return str(httpx.URL(AUTHORIZE_URL, params=params))


def _json_body(response: httpx.Response) -> dict[str, Any]:
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise GitHubOAuthError("response too large")
    try:
        body = response.json()
    except ValueError as exc:
        raise GitHubOAuthError("response was not JSON") from exc
    if not isinstance(body, dict):
        raise GitHubOAuthError("response was not a JSON object")
    return body


def exchange_code(settings: Settings, code: str) -> str:
    """Exchange the authorization code for an access token.

    Returns the raw token to its single caller. Nothing here logs the code,
    the client secret, or the token.
    """
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as client:
            response = client.post(
                CODE_EXCHANGE_URL,
                data={
                    "client_id": settings.github_client_id,
                    "client_secret": settings.github_client_secret.get_secret_value(),
                    "code": code,
                    "redirect_uri": settings.github_redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise GitHubOAuthError("token endpoint unreachable") from exc

    if response.status_code != httpx.codes.OK:
        raise GitHubOAuthError(f"token endpoint returned {response.status_code}")

    body = _json_body(response)
    # GitHub reports OAuth failures with HTTP 200 and an "error" key.
    if body.get("error"):
        raise GitHubOAuthError("token endpoint reported an error")

    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise GitHubOAuthError("token endpoint returned no access token")
    return token


def fetch_profile(access_token: str) -> GitHubProfile:
    """Read the authenticated user's profile with a short-lived token."""
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as client:
            response = client.get(
                USER_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {access_token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
    except httpx.HTTPError as exc:
        raise GitHubOAuthError("user endpoint unreachable") from exc

    if response.status_code != httpx.codes.OK:
        raise GitHubOAuthError(f"user endpoint returned {response.status_code}")

    body = _json_body(response)
    github_id = body.get("id")
    login = body.get("login")
    if not isinstance(github_id, int) or isinstance(github_id, bool):
        raise GitHubOAuthError("profile has no numeric id")
    if not isinstance(login, str) or not login:
        raise GitHubOAuthError("profile has no login")

    display_name = body.get("name")
    avatar_url = body.get("avatar_url")
    return GitHubProfile(
        github_id=github_id,
        login=login,
        display_name=display_name if isinstance(display_name, str) and display_name else None,
        avatar_url=avatar_url if isinstance(avatar_url, str) and avatar_url else None,
    )
