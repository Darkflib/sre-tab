# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Operator CLI (`sre-tab`): seed the v1 source catalogue and topic
  taxonomy, list/add/enable/disable sources and topics, expand a Medium
  tag into its own source, and a per-source refresh-status view that
  exits non-zero when an enabled source is failing.
- `source_status` table recording each source's last fetch, last
  success, last error, and consecutive failures, so status survives a
  restart and can be read by a process other than the one fetching.
- `COOKIE_SECURE` setting, defaulting to true, for development against a
  non-localhost host over plain http.
- PostgreSQL integration suite (`tests/postgres/`), opt-in on
  `SRE_TAB_POSTGRES_URL` and run in CI against a service container.

### Changed

- The feed scheduler now starts with the application, and
  `/api/v1/healthz` reports its readiness.
- Bookmarked feed items are exempt from retention pruning.
- One transaction convention across the codebase: whoever opens the
  session commits it. Services and store helpers flush only.
- `deploy/Caddyfile` trusts the gateway as a proxy and
  `FORWARDED_ALLOW_IPS` names both hops, so per-IP rate limiting sees
  the real client address instead of collapsing into one bucket.
- `ALLOWED_GITHUB_IDS` ships with the initial operator allow-list rather
  than empty.

### Fixed

- `GET /auth/github/callback` returned a 422 validation error when a
  user declined authorisation on GitHub; it now redirects to the landing
  page with a message.
- `429` is documented on the rate-limited auth routes, and
  `frontend/openapi.json` regenerated to match.

- Phase 0 foundation: repo baseline, tooling (`uv`, Ruff, mypy, pytest,
  Bandit, pre-commit), settings, structured logging with request IDs and
  secret redaction, complete SQLAlchemy 2.x schema with a single initial
  Alembic revision, FastAPI app shell (security headers, CSRF primitive,
  health probe registry), and the full `/api/v1` contract as Pydantic
  schemas plus `501` stub routes.
