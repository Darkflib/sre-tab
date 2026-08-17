# Worklog

Newest entries first. One entry per meaningful unit of work; note decisions
and deviations, not just activity.

## 2026-08-17 — The filter model under test, and what it exposed

72 Vitest tests over `src/feed/filters.ts` and `src/feed/volume.ts`, the
cheap half of the frontend-coverage item and the half the roadmap said to
take first. It was right about the ordering for the stated reason — both
modules import types only, so they needed no DOM, no request mocking, and
no new dependency — and the suite goes from 114 to 186 tests still running
in under half a second.

The subject is one distinction with three meanings. `null` is "no
override, use my saved selection"; `[]` is "the user deselected
everything". The server completes the set: `_effective_sources` honours an
explicit `[]` verbatim and returns an empty page, but an *empty saved
selection* means the instance defaults, which is everything. So the same
empty array means "nothing" on the request side and "everything" on the
saved side, and which one you get is decided by nothing more visible than
which side of a `??` it sits on.

Mutation-tested rather than merely run, on the theme suite's precedent:
thirteen mutations, each the plausible mistake rather than an arbitrary
one — `selectsNothing` rewritten as a falsy check, an empty selection
serialised as an absent parameter, `filterKey` losing its sort, the
dominance comparison becoming exclusive, the twelve-item floor slipping to
eleven. All thirteen fail the suite.

**Two of those tests were wrong, and review caught it.** They pinned a
comma in a slug being split by the URL, and `filterKey`'s `*` and `+`
sentinels aliasing, as documented assumptions — on the stated grounds that
slugs are kebab-case and so cannot contain either character. That premise
was never checked. It is false: `add_source` validates uniqueness, the
feed URL, and the refresh interval but not the slug's shape, `add_topic`
validates uniqueness alone, and both columns are plain `String(64)` with
no CHECK. The only strict slug pattern in the tree guards the Medium tag
expansion, where the value is interpolated into a feed URL, and says
nothing about the general creation paths.

So both were reachable defects, and the tests had made them expected
results — which would have failed whoever later fixed them. Worth naming
the mechanism, because the repository has been careful about exactly this
elsewhere: the constraint was inferred from what the seeded catalogue
looks like, then written down as though it were enforced. A catalogue
where every slug is kebab-case and a system that requires it are not the
same claim, and only one of them was true.

`filterKey` is fixed — it encodes as JSON, so no slug can alias one
selection onto another's cache entry, which mattered because
`usePagedResource` refetches only when the key changes and an alias
therefore serves the previous filter's items. The comma case is now
`it.fails` asserting the behaviour we want: it records the gap without
pinning the defect, and errors with "expected to fail but passed" the day
someone closes it. Verified by applying the repair and watching the marker
trip. The choice between enforcing a slug format and preserving arbitrary
slugs in the URL is on the roadmap, costed both ways.

**The tests found a live bug they do not fix.** `FilterBar`'s "Save as my
default" writes `effectiveSelection`'s result into preferences, which
carries `[]` across from the override side to the saved side, where it
means the opposite. Deselect every source, save, and the feed goes from
empty to the entire catalogue — the user's "show me nothing" stored as
"show me everything", in two clicks, with no error. Left unfixed
deliberately: disabling the control while `selectsNothing` is the narrow
answer, but what saving an empty selection *ought* to mean is a product
decision, and inventing one inside a testing task is how a defect becomes
a behaviour. It is on the roadmap with the reproduction.

This also became the repository's first pull request, which finally
exercised the corrected branch-protection rule on a real merge path
instead of by set-differencing the required contexts against the reported
ones. All eight reported and passed, and the request came back
`MERGEABLE` — the property the set-difference could only infer. The
excluded `Publish, sign, and attest image` reported as `SKIPPED` rather
than not reporting at all, which rules out the failure mode the broken
rule actually had; whether protection would *accept* a skipped conclusion
is still unmeasured, and can only be measured by taking the risk the
exclusion exists to avoid. `mergeStateStatus` was `UNSTABLE` rather than
`CLEAN`, which turned out to be CodeRabbit posting a non-required check
and not a protection failure — worth knowing before someone reads that
word as a problem.

Two documentation corrections alongside: `CONTRIBUTING.md` carried the
same paragraph about `audit` and `sast` twice, and the roadmap still
listed "make `Docs` a required check" as pending when it had landed inside
the branch-protection rewrite — that write replaced the context list
wholesale, so both of its checks came along and nobody went back to strike
the item.

## 2026-08-17 — Licence, and the notes brought up to date

MIT `LICENSE` added, matching the declaration that had been sitting in
`pyproject.toml` with nothing in the repository to back it. Backfilled the
worklog entries below, which had stopped after Phase 2 while the work did
not, and the changelog entries for the decompression fix, the frontend
suite, the docs workflow, and the theme contrast work.

Worth recording why the backlog happened: five agents were committing in
parallel to one working tree, and `CHANGELOG.md`/`WORKLOG.md` are owned by
nobody in that arrangement, so each agent correctly declined to race for
them. Shared-file ownership needs assigning explicitly, the same way the
code paths were.

## 2026-08-17 — Branch protection was never enforcing anything

The rule was created with the job *keys* from `ci.yml` — `python`,
`postgres`, `audit`, `frontend`, `container`. GitHub keys a required check
on the check-run **context**, which for Actions is the job's `name:`
whenever one is set, and every job here sets one. So all five required
contexts named checks that had never reported and never could.

It failed safe — a pull request waits on a status that never arrives rather
than merging unchecked — but the real checks were not required either, and
80 commits of direct pushes never consulted the rule, so nothing surfaced
it. Now set to the eight reported check-run names, excluding
`Publish, sign, and attest image`, which only runs on push to `main`.
Verified by set-differencing the required contexts against the check-runs
the repository actually reports: empty, in the direction that matters.

The general trap: a job rename is a branch-protection change. Nothing in
the repository can detect it, because protection lives in GitHub's
settings and not in a file anyone reviews.

## 2026-08-17 — Documentation, dark mode, supply chain

Three parallel workstreams after the release-blocking fixes.

- **Supply chain.** Application image digest-pinned with a promotion
  script that refuses to write a digest cosign cannot verify; cosign
  keyless signing, SLSA provenance, and an SPDX SBOM, all bound to the
  digest. Admission verification is *not* possible and the gap is
  measured rather than asserted: podman's `sigstoreSigned.fulcio` policy
  requires `oidcIssuer` **and** `subjectEmail`, and a GitHub Actions
  keyless certificate carries a URI SAN, so the identity cannot be
  expressed. A `{"type":"reject"}` policy on the same repository does
  refuse the pull, which is how we know the machinery works and the
  identity is the blocker.
- **Documentation.** README from 58 to 311 lines, `CONTRIBUTING.md`, and a
  workflow that extracts the quickstart from the README and runs it on a
  clean checkout. Checking the prose found eight things documented and
  wrong, the sharpest being `deploy/README.md`'s claim that the fetcher is
  the only component making outbound requests — the OAuth flow also calls
  GitHub, so an egress policy allowing only feed hosts would have broken
  sign-in.
- **Dark mode.** Verified rather than assumed, and it was not as
  advertised: body text passed everywhere, which is why it read as done,
  while every interactive boundary failed WCAG AA. 114 tests added, then
  mutation-tested by reverting tokens and drifting the palette to confirm
  they fail.

## 2026-08-17 — Phase 3 verification

Four parallel read-only passes over the integrated tree: an adversarial
security review, SAST, an acceptance walk of the v1 criteria, and a
deployment validation on a real Debian 13 host with podman 5.4.2.

The Linux pass is the one that earned its cost. Two release-blockers
existed that no amount of macOS testing would have found, and both trace
to the same root cause — `no_new_privs` blocks the AppArmor profile
transition crun performs on exec, after which AppArmor denies signals
between the resulting profiles. PostgreSQL wedged for the full five-minute
timeout; uvicorn exited 1 on every clean stop. `smoke.sh` already set that
flag, so it *was* under test — it just does not trigger under Docker on
arm64.

Also found there: Caddy's pinned `.20` sat inside the network's dynamic
IPAM pool, so an unrelated restart could hand that address to another
container and leave Caddy in a permanent restart loop; and backup dumps
were group-readable by `systemd-journal`, because gid 999 is `postgres`
inside the postgres image but `systemd-journal` on Debian.

Acceptance measured the feed at 12.7 ms p95 against a 400 ms target, and —
more convincingly than the number — flat from 5,000 to 100,000 items,
which is what proves the keyset pagination rather than the hardware.
`EXPLAIN` confirms the index is used on PostgreSQL in every query shape;
on SQLite it is not, once a filter is present. Recorded rather than fixed:
PostgreSQL is the production engine.

## 2026-08-17 — Phase 1 fan-out

Five agents in parallel on disjoint paths: auth and sessions, ingest and
scheduling, feed and user state, frontend, and build/deploy/CI. Phase 0's
contract — complete models, complete schemas, and `501` stubs so
`openapi.json` was real on day one — is what made that possible; without
it five agents would have invented five incompatible data layers.

Three conflicts were designed out rather than discovered: the Alembic
revision graph (Phase 0 wrote the only migration, Phase 1 wrote none),
`pyproject.toml` (the dependency set was pinned up front, and no agent
ran `uv add`), and the router aggregator (pre-wired to the stubs, so no
agent edited a shared file). One conflict was *not* designed out and had
to be fixed mid-flight: `/me` routes live in one module owned by the auth
agent while the preferences logic belonged to the API agent, resolved with
a frozen service-signature seam committed before either started.

What the parallelism actually cost: agents share one working tree, and
`git commit` takes the whole index, so one agent's staged files landed in
another's commit twice. Pathspec-scoped commits fixed it. Uniform git
authorship also means no commit records *which* agent wrote it, which
later made two agents each credit the other with the same work.

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
