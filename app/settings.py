"""Application configuration.

Every configurable value flows through :class:`Settings`. This module is
Phase 0 property: new variables are an escalation, not an edit (AGENTS.md).
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core -----------------------------------------------------------
    database_url: str = "sqlite:///./dev.db"
    app_base_url: str = "http://localhost:8000"

    # Swagger UI at /docs. Default-closed: a deployment that was not derived
    # from deploy/app.env.example — a container run by hand, a second
    # instance, anything that inherits only the defaults — should not publish
    # an interactive client against its own API because nobody said not to.
    # Development opts in with DOCS_ENABLED=true.
    #
    # /api/v1/openapi.json is unaffected and served either way: publishing the
    # schema is a v1 requirement. This flag governs the UI, not the contract.
    docs_enabled: bool = False

    # --- GitHub OAuth ---------------------------------------------------
    github_client_id: str = ""
    github_client_secret: SecretStr = SecretStr("")
    github_redirect_uri: str = "http://localhost:8000/api/v1/auth/github/callback"

    # Comma-separated numeric GitHub user IDs. An empty list denies
    # everyone: sign-in is allow-list only in v1 (PLAN decision 1).
    allowed_github_ids: Annotated[list[int], NoDecode] = []

    # --- Sessions and CSRF ----------------------------------------------
    # Random per process by default so a dev instance is never signed with
    # a published value; production MUST set SESSION_SECRET explicitly or
    # sessions and CSRF tokens are invalidated on every restart.
    session_secret: SecretStr = Field(default_factory=lambda: SecretStr(secrets.token_urlsafe(48)))
    session_ttl_days: int = Field(default=14, ge=1)
    session_cookie_name: str = "session"
    csrf_cookie_name: str = "csrftoken"
    csrf_header_name: str = "X-CSRF-Token"

    # `Secure` on the session, CSRF, and OAuth-state cookies. True in
    # every deployment — the PRD requires it and the reverse proxy
    # terminates TLS.
    #
    # The escape hatch exists for one narrow case: development against a
    # host that is not `localhost`, over plain http. Browsers exempt
    # localhost from the Secure rule, so ordinary local work needs
    # nothing here; a colleague testing against http://dev-box.lan does,
    # and without this the browser silently drops every cookie and
    # sign-in looks broken rather than misconfigured. Setting it false on
    # anything reachable from a network hands the session cookie to
    # anyone on the path.
    cookie_secure: bool = True

    # --- Feed retention and source refresh ------------------------------
    feed_retention_days: int = Field(default=90, ge=1)
    source_refresh_enabled: bool = True
    source_default_refresh_minutes: int = Field(default=30, ge=1)
    source_fetch_timeout_seconds: float = Field(default=10.0, gt=0)
    source_fetch_max_bytes: int = Field(default=5_242_880, gt=0)
    source_fetch_max_redirects: int = Field(default=3, ge=0)
    source_fetch_user_agent: str = "DevNewsDashboard/0.1 (self-hosted feed aggregator)"

    # --- Logging --------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("allowed_github_ids", mode="before")
    @classmethod
    def _split_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.split(",") if part.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
