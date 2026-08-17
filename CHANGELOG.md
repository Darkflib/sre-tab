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

### Security

- Database dumps are written `0600` under `umask 077`, and
  `/srv/sre-tab/backups` is created `0700` rather than `0750`. The
  directory is owned by gid 999, which is `postgres` inside the postgres
  image but `systemd-journal` on Debian 13 — so on that distribution the
  old modes let anyone with journal access read every user record in the
  instance. The comment claiming otherwise is corrected.
- The application image strips every setuid and setgid bit at build time
  (eleven of them in `python:3.12-slim-trixie`), which is what lets
  `sre-tab.container` drop `NoNewPrivileges=true` without losing the
  protection that flag was there for.
- The CSRF token is bound to the session it was issued for. Previously
  the signature proved only that the server had minted the token, so a
  validly signed token minted for no session at all was accepted on
  another user's session.
- `SOURCE_FETCH_TIMEOUT_SECONDS` now bounds body streaming, not just the
  handshake and the gaps between redirect hops. A server dribbling one
  byte at a time held a scheduler tick — and so every source's refresh —
  for as long as the size cap allowed.
- Rendered tracebacks are redacted, and frame locals are no longer
  emitted at all. structlog's `ExceptionDictTransformer` defaults to
  `show_locals=True`, and the redaction processor ran ahead of traceback
  rendering, so anything it produced bypassed redaction.
- Non-ASCII credential material is refused with a 403 rather than
  crashing the request: `hmac.compare_digest` raises on non-ASCII `str`
  instead of returning false, and headers arrive latin-1 decoded.
- The SSRF guard's host normalisation can no longer raise outside the
  guard's own error type, and `source add` refuses obfuscated IP
  literals that only become literals once the host is normalised
  (`https://0x7f.0.0.1./rss` and family) instead of storing them and
  failing at fetch time.

### Fixed

- PostgreSQL now starts. `NoNewPrivileges=true` on `sre-tab-db.container`
  stopped it ever reaching `pg_isready`: podman's AppArmor profile denies
  signal delivery under `no_new_privs` on Debian 13, so `gosu` live-locked
  in a `sched_yield()` loop for the full five-minute `TimeoutStartSec`, on
  every start rather than only the first.
- `sre-tab-web` no longer fails permanently after an unrelated restart.
  Caddy's pinned `10.89.61.20` sat inside the network's dynamic pool, so
  any container that happened to be handed that address left Caddy looping
  on `IPAM error: requested ip address 10.89.61.20 is already allocated`.
  `IPRange=` now confines dynamic allocation to `.32-.254`.
- A clean `systemctl stop` leaves `systemctl --failed` empty. uvicorn's
  re-raise of the captured signal returned `EPERM` under the same AppArmor
  interaction, so every deliberate stop recorded `exit-code/1` and made
  the project's stated failure-surfacing mechanism useless.
- The `/api/v1/healthz` readiness check is bounded at five seconds. A
  *frozen* database — as opposed to a stopped one — never answers and
  never errors, so the probe used to hang past 25 seconds and a sick
  dependency was indistinguishable from a sick application.
- `GET /auth/github/callback` returned a 422 validation error when a
  user declined authorisation on GitHub; it now redirects to the landing
  page with a message.
- An absurdly large cursor integer answered 500 instead of the
  documented 400: `int()` is unbounded where `timedelta` is not.
- `429` is documented on the rate-limited auth routes, and
  `frontend/openapi.json` regenerated to match.

- Phase 0 foundation: repo baseline, tooling (`uv`, Ruff, mypy, pytest,
  Bandit, pre-commit), settings, structured logging with request IDs and
  secret redaction, complete SQLAlchemy 2.x schema with a single initial
  Alembic revision, FastAPI app shell (security headers, CSRF primitive,
  health probe registry), and the full `/api/v1` contract as Pydantic
  schemas plus `501` stub routes.
