# Worklog

Newest entries first. One entry per meaningful unit of work; note decisions
and deviations, not just activity.

## 2026-08-17 — Phase 3 security remediation

Six reproduced findings from the adversarial review. The SSRF guard and
the address pinning withstood the whole campaign — ~40 redirect `Location`
forms, NAT64/6to4/Teredo, split DNS answer sets, obfuscated literals,
socket-level checks that `Host` and SNI survive to the wire, TOCTOU, XXE
— and cross-user isolation held completely. Neither was touched beyond
the one line noted below.

- **The fetch deadline did not bound the body.** `httpx.Timeout` is
  per-operation and the deadline was only re-checked between redirect
  hops, so a dribbling server was limited by max-bytes ÷ dribble-rate
  rather than by time. Measured at twenty minutes for a "0.3 second"
  fetch. The scheduler ticks sources serially under `max_instances=1`, so
  the cost was every source's refresh, then readiness, then a restart
  loop. `_read_capped` now takes the deadline and re-checks it per chunk.
- **The CSRF token was not bound to the session.** The signature proved
  the server minted the token, not who for — a token minted with no
  session in existence was accepted on another user's session. It now
  commits to `sha256(session_token)`, verified with no extra query. The
  cookie stays script-readable, because it has to be; the binding is what
  adds the security, not secrecy. Rotation falls out for free.
- **`hmac.compare_digest` raises on non-ASCII `str`** rather than
  returning false, and headers arrive latin-1 decoded, so one 0x80-0xff
  byte was an unauthenticated 500. Three call sites, not the two
  reported: the third is `StateStore.consume`, reached with a three-part
  state token. `compare_secret` compares bytes instead. The OAuth variant
  now also reaches the failure limiter it used to crash in front of.
- **Traceback rendering bypassed redaction entirely.** `redact_sensitive`
  ran *before* `dict_tracebacks`, so it inspected an event that did not
  yet contain what it exists to remove — and structlog 26.1.0 defaults
  `show_locals=True`. Latent only because the sole `exc_info` call sites
  are in the scheduler; `code` and `client_secret` are live locals on the
  OAuth path, so the first `log.exception` there would have written both
  in cleartext. Ordering reversed, locals off.
- **`OverflowError` on an absurd cursor integer** answered 500 rather
  than the documented 400.
- **`copy_with` could raise `httpx.InvalidURL` out of the guard**, which
  was classified as `error_class="InvalidURL"` instead of an unsafe
  target. Wrapped — the only change made to `urlguard.py`.

Two hardening items, both wider than reported:

- `validate_feed_url` ran `check_static` once, and `check_static` judges
  IP literals *before* normalising the host. A host can only become an
  obfuscated literal after normalisation, so `https://0x7f.0.0.1./rss`,
  `https://127.1./rss`, `https://0177.1./rss`, and `https://0.0.0.0./rss`
  were all stored at `source add` time and refused hours later at fetch
  time — the exact failure the function exists to prevent. Config-time
  validation is now required to be a fixpoint, which catches the family
  rather than the instance. No SSRF was reachable: `validate` re-judges
  the normalised host as a literal before resolving, and always did.
- `tests/conftest.py` overrides `get_current_user`, so `authed_client`
  never sends a session cookie and `CSRFMiddleware` never fired: the
  whole of `tests/api/` ran with CSRF unenforced. Confirmed by neutering
  `require_csrf` and watching the suite stay green. The override stays —
  it is the right trade for tests about what routes do — and
  `tests/api/test_csrf_enforcement.py` covers one mutating endpoint per
  module against a real session instead.

## 2026-08-17 — Phase 2 integration

- **Scheduler wired.** `create_app` calls `install_scheduler`, so the
  refresh loop starts with the application and `/api/v1/healthz` carries
  its readiness probe. Root test settings disable source refresh;
  without that every test using the `app` fixture would spawn a real
  APScheduler thread and fetch live feeds.
- **One transaction convention**, recorded in AGENTS.md: whoever opens
  the session commits it. The tree had three — routes committing,
  mutation services self-committing, and `preferences` flushing while
  its docstring claimed `get_db` owned the boundary, which it never did.
  Chosen for composability: the OAuth callback already needs four writes
  in one transaction, and a self-committing service cannot be called
  twice in one unit of work.
- **Bookmarked items are never pruned.** A bookmark is an explicit "keep
  this" and must not evaporate on a retention schedule the user never
  set. Needed no DDL — a `NOT EXISTS` predicate on the delete. Read
  marks still cascade; only bookmarks confer immunity, and immunity ends
  when the bookmark does.
- **`source_status`** (revision `29038199b328`): scheduler-written
  refresh state, 1:1 with `sources` and deliberately not columns on it,
  so the operator and the refresh loop never contend and
  `sources.updated_at` keeps its meaning. The in-process registry writes
  through to it, which is what lets a separate CLI process read status,
  and persisting `last_fetched_at` stops a restart treating the whole
  catalogue as due at once.
- Migration verified `upgrade`/`downgrade` on SQLite and on a real
  PostgreSQL 18 (Docker; podman is not installed on this machine),
  against both an empty and a populated database.
- **Seed catalogue and operator CLI** (`sre-tab`, argparse, no new
  dependency): the PLAN catalogue and taxonomy, source and topic
  management, `add-medium-tag`, and a refresh-status view that exits
  non-zero when a source is failing. Feed URLs are validated by the SSRF
  guard's DNS-free half at *add* time.
- **`tests/postgres/`**, opt-in on `SRE_TAB_POSTGRES_URL` and gated in
  CI: `pg_try_advisory_lock` against a live server, the PostgreSQL
  `ON CONFLICT` branches, and the migration on the engine it will
  actually run on.
- **The client-address chain was broken**, and the fix was not where the
  documentation said. Caddy 2.7+ refuses an `X-Forwarded-For` from an
  untrusted peer and *replaces* it, so the outer TLS proxy setting the
  header bought nothing: every request reached uvicorn as one address
  and per-IP rate limiting was global with no symptom. Fixed at both
  ends — `trusted_proxies` in the Caddyfile so Caddy appends, and
  `FORWARDED_ALLOW_IPS` naming both hops so uvicorn's right-to-left walk
  reaches the real client. Verified end to end against real uvicorn and
  real Caddy under Docker.
- `GET /auth/github/callback` no longer 422s on GitHub's user-denial
  redirect; `COOKIE_SECURE` added for plain-http dev on a non-localhost
  host; 429 documented and `frontend/openapi.json` regenerated;
  `ALLOWED_GITHUB_IDS` seeded with the three verified operator IDs and
  documented as the fail-closed trap it is on first deploy.
- `deploy/scripts/smoke.sh` extended: it now asserts the scheduler probe
  is present and on `postgres-advisory`, seeds through the CLI, checks
  the CLI refuses hostile targets, and demonstrates that liveness and
  readiness are different answers by taking the database away.

## 2026-08-17 — Phase 0 foundation

- Repo initialised (`main`), baseline docs, `.gitignore`.
- `uv` project with the complete pinned dependency set; Ruff, mypy, pytest,
  Bandit, and pre-commit configured.
- Settings (`app/settings.py`) and structlog JSON logging with request-ID
  middleware and secret redaction (`app/logging.py`).
- Full ORM layer for all twelve PRD entities, sync SQLAlchemy 2.x, single
  initial Alembic revision; upgrade/downgrade and `alembic check` verified
  on SQLite and on a real PostgreSQL 16 (throwaway Docker container —
  podman is not installed on this machine). The autogenerated revision
  needed hand-editing for dialect neutrality: SQLite-compiled boolean and
  timestamp server defaults would have failed on PostgreSQL.
- App factory, security-headers middleware, CSRF primitive (double-submit
  cookie, HMAC-signed), health probe registry, `get_current_user` stub.
- Complete Pydantic schemas and 501 stub routes for all twelve endpoints;
  `/api/v1/openapi.json` complete.
- Smoke test suite and root fixtures.
