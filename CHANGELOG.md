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
- Published images are signed with cosign using GitHub's OIDC identity
  (no key to store or rotate), and carry SLSA build provenance and an
  SPDX SBOM, all bound to the image digest rather than to a tag.
- `deploy/scripts/promote.sh` promotes a published build: it resolves a
  commit to the digest the registry serves, refuses to write one cosign
  cannot verify, and pins all three application units together.
- `deploy/scripts/verify-image.sh` checks the signature, the provenance,
  and the SBOM for any digest — used by CI on every run, by the promotion
  step before it writes, and by an operator before a restart.
- npm audit in CI: the production tree at high and above is a gate, the
  full tree including dev dependencies is reported without failing.
- Semgrep (`sast` job), guarded so that a run which errored or scanned no
  files fails the build rather than passing with no findings.
- Frontend test suite (114 Vitest tests) covering theme resolution, the
  anti-flash script, and the contrast ratios of the design tokens — the
  first tests the client has had — and they run in CI.
- 73 further Vitest tests over the feed's filter model
  (`src/feed/filters.ts`) and volume signals (`src/feed/volume.ts`),
  taking the suite to 186. They pin the distinction between "no override"
  (`null`) and "nothing selected" (`[]`), including its survival through
  the URL, and the thresholds behind the high-volume flag and the
  dominance notice.
- `LICENSE` (MIT), matching the declaration that was already in
  `pyproject.toml` but had no corresponding grant in the repository.
- `CONTRIBUTING.md`, and a `Docs` workflow that extracts the README's
  quickstart from the README itself and executes it on a clean checkout
  on every push. Two documented procedures here have been wrong while
  reading perfectly, so the documentation is executed rather than
  proofread.

### Changed

- **The application image is pinned by digest.** The three application
  units tracked `:latest` with `Pull=newer`, so any restart adopted
  whatever CI had last pushed to main. They now pin
  `:sha-<commit>@sha256:<digest>` with `Pull=missing`; upgrading is a
  reviewed commit produced by `promote.sh`, not a side effect of
  restarting. See the upgrade procedure in `deploy/README.md`.
- **`DOCS_ENABLED` now defaults to false.** A deployment that inherits
  the defaults no longer serves Swagger UI at `/docs`; set
  `DOCS_ENABLED=true` to opt in. `/api/v1/openapi.json` is unaffected and
  served either way.

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
- Dark and light themes meet WCAG AA on interactive boundaries, not just
  on body text. Button, input, and inactive-chip borders sat at 1.80:1 in
  dark and 1.95:1 in light against 1.4.11's 3:1, and read-card summary
  text at 3.22:1 against 1.4.3's 4.5:1 — the kind of failure a screenshot
  does not show, because the text on top of them was always legible. A
  `--focus-halo` token also separates the focus ring from the fill it
  sits on: in dark, `--focus` and `--accent` were the same colour, so a
  focused active chip was a glow rather than a ring.

### Security

- Feed fetches refuse content-codings. The size cap counted bytes `httpx`
  had already decompressed and `Content-Length` was checked against the
  compressed length, so neither bounded what actually got allocated: a
  20 KB body materialised 21 MB, and because a decoder is built per
  comma-separated `Content-Encoding` value, stacked codings reached a
  gigabyte from a few hundred bytes on the wire. Under the unit's
  `MemoryMax=768M` that is a cgroup kill of the process hosting both the
  API and the scheduler, and since the process is killed rather than
  raising, the per-source backoff never engaged — `Restart=always` plus
  an immediate first tick made it a loop. The fetcher now asks for
  `Accept-Encoding: identity` and refuses any coding an origin sends
  regardless; the request is a courtesy, the refusal is the enforcement.
  All seven catalogue feeds honour it, measured against the live origins,
  at a cost of about 554 KB per full refresh.
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

- The feed's cache key can no longer alias two different filters onto one
  entry. `filterKey` joined the selection with `+` and wrote `*` for "no
  override", so a source slug of `*`, or the pair `a`/`b` against a single
  slug `a+b`, produced the same key — and since the paged resource only
  refetches when the key changes, the second selection was served the
  first's items. Nothing constrains a slug's shape at any creation path,
  so this was reachable rather than theoretical; the key is now encoded as
  JSON.
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
