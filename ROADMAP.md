# Roadmap

Work deliberately deferred past v1. Items are grouped by why they were
deferred, not by size. See [prd-v1.md](prd-v1.md) for v1 scope and
[PLAN-v1.md](PLAN-v1.md) for how it was built.

Items marked **landed** stay listed rather than being deleted: the reasoning
that put them here is usually still worth reading, and a roadmap that only
ever grows tells you nothing about what moved.

## Open work

The index below is the open subset of the sections that follow: landed items
are left out, and where an item is only partially landed, only the part that
remains is named here. It links to sections, not to individual bullets — the
bullets below are not headings, so there is nothing for a fragment link to
name — and it does not restate any item's reasoning; read the section for
that.

- [Supply-chain hygiene](#supply-chain-hygiene)
  - Verify signed images at container start, not just before the fact.
  - JS/TS taint-flow coverage is thinner than the Python equivalent's.
  - The build path's dependency on `codeload.github.com` — vendor the
    actions, or replace `download-syft` with a registry pull.
- [Security findings this deployment absorbs](#security-findings-this-deployment-absorbs)
  - Cut the deployment over to the three least-privilege database roles —
    `restore.sh` and the smoke test use them now, and every Quadlet unit
    still connects as the superuser.
  - The OAuth state cookie can be overwritten from a sibling subdomain.
  - `upsert_user` has no conflict handling for a concurrent first sign-in.
- [API surface](#api-surface)
  - Serve `/api/v1/openapi.json` from the committed file in
    `deploy/Caddyfile`, not from the running application.
- [Scaling](#scaling)
  - Shared state store for OAuth state and the rate limiters.
  - Separate scheduler worker.
  - Rate limiting keyed on a trusted client address — fragile pending a
    shared store.
- [Operations](#operations)
  - Off-host backups.
  - An `OnFailure=` alert unit.
  - Release hygiene: no Release against `v1.0.0`, and no versioned image tag
    a self-hoster can pull.
  - Frontend coverage for `src/api/client.ts`, and for the parts of
    `usePagedResource` that need a DOM — its decision logic is covered now,
    its effects are not.
- [Things that are true but unproven](#things-that-are-true-but-unproven)
  - The backup timer's `Persistent=true` catch-up, demonstrated rather than
    assumed.
  - The fetcher's accept-a-redirect branch, against a live server.
  - Quadlet runtime under a real soak — still just two cold installs and a
    handful of restarts.
  - The ~20s of deploy outage after `systemctl` returns — cause still
    unknown.
  - The `>=3.12` floor, checked by hand once and by CI never.
  - The Containerfile's setuid inventory, counted on the 3.12 base image.
- [Documentation](#documentation)
  - `deploy/README.md` in CI, which needs a journald-capable runner, and
    the upgrade sequence, still unexecuted.
- [Repository](#repository)
  - Issue and pull-request templates, and the repository's own
    discoverability — no topics, no homepage, no social preview.
- [Product](#product)
  - Per-device preferences (v2).
  - Non-RSS sources.
  - Richer authorisation.
  - A `compose.yaml`, for the deployment that sits between the quickstart
    and the quadlets.
  - A denied sign-in should land on the page, not on a JSON 403.
  - An admin surface for `is_admin`, which nothing sets and nothing reads.
  - Search over the retained items.
  - A data export, and OPML in the CLI.
  - First-screen polish: source icons, mark-all-read, per-source freshness,
    and a second mobile breakpoint.
  - A `/metrics` endpoint.

<a id="supply-chain-hygiene"></a>
## Supply-chain hygiene

Raised by the Phase 3 SAST pass. The dependency trees came back clean — zero
CVEs and zero verified secrets across the full git history — so this section
is about the pipeline, not the code.

- **Digest-pin the application image** — **landed.** `sre-tab.container`,
  `sre-tab-migrate.container`, and `sre-tab-assets.container` tracked
  `:latest` with `Pull=newer`, the single unpinned link in an otherwise fully
  pinned chain: a restart for any reason — a reboot, an OOM kill, clearing a
  stuck connection — silently adopted whatever CI had last pushed to main, so
  the running version was decided by whoever merged most recently. All three
  now pin `:sha-<commit>@sha256:…` with `Pull=missing`, and an upgrade is a
  reviewable commit rather than a restart. `deploy/scripts/promote.sh` is the
  promotion step; it resolves a commit to the digest the registry serves,
  refuses to write one cosign cannot verify, and moves all three units
  together, because migrations, the application, and the frontend assets ship
  in one image precisely so they cannot skew. CI asserts both that the three
  agree and that the digest they name is signed.
- **Sign and verify** — **partially landed, and the gap is worth naming.**
  Images are signed with cosign keyless against GitHub's OIDC identity, and
  SLSA provenance is attested. What does *not* exist is verification at
  admission, and it is not an oversight: podman's `containers-policy.json`
  `sigstoreSigned.fulcio` block requires both `oidcIssuer` and `subjectEmail`,
  and a GitHub Actions keyless certificate carries a URI SAN
  (`https://github.com/Darkflib/sre-tab/.github/workflows/ci.yml@refs/heads/main`)
  rather than an email — so this identity cannot be expressed in a podman
  signature policy at all. Verification therefore happens before the fact,
  four times: the publish job re-verifies what it just pushed, `promote.sh`
  refuses to pin a digest cosign cannot verify, CI re-verifies the pinned
  digest on every push, and `deploy/scripts/verify-image.sh` lets an operator
  check before a restart. **A container start still checks nothing.** Closing
  that needs either a policy format that can express a URI SAN or a
  verification step wired into the units themselves.
- **Generate an SBOM** — **landed.** syft produces SPDX JSON from the pushed
  image, `actions/attest-sbom` publishes it as an attestation alongside the
  image in the registry, and the document is also retained as a workflow
  artefact.
- **Extend Renovate to the CI workflows** — **landed, and the original premise
  was wrong.** GitHub Actions were never unwatched: `config:recommended`
  enables the Actions manager, and the dependency dashboard has been listing
  ci.yml's actions — with pending majors — all along. The genuine blind spots
  were narrower and are now covered by custom managers: an image named inside
  a `run:` script (the Caddy image the Caddyfile validation uses) is invisible
  to every built-in manager, and so was the pinned semgrep version.
  `helpers:pinGitHubActionDigests` keeps new actions arriving as commit SHAs
  with the version in a trailing comment, and `.github/workflows/docs.yml`
  reuses ci.yml's exact pins so both move in one PR.
- **Fail CI on a non-empty Semgrep `errors[]`** — **landed.** A `sast` job now
  fails the build on a non-empty `errors[]` *and* on `paths.scanned == 0`. The
  original failure was reproduced before the guard was written: `p/bash` 404s,
  and that aborted the whole scan while still emitting a well-formed report
  with `results: 0`, `scanned: []`, and exit 0 — a green gate that had scanned
  nothing. Rulesets are named individually rather than via `auto`.
  `p/dockerfile` is deliberately absent: semgrep does not recognise a file
  named `Containerfile`, so it would have run zero rules over zero files while
  looking like coverage.
- **Supplement JS/TS coverage** — **partially landed, and half the original
  evidence was a false conclusion.** The canary's AWS key went unflagged
  because it was AWS's own documentation key, `AKIAIOSFODNN7EXAMPLE`, which
  `p/secrets` allow-lists on purpose; a realistic `AKIA…` key is flagged. The
  DOM sinks genuinely were missed by the OSS registry rules and still are,
  which is what `.semgrep/frontend-dom-sinks.yml` exists for — three local
  rules covering `innerHTML`/`outerHTML`/`insertAdjacentHTML`,
  `dangerouslySetInnerHTML`, and `eval`/`new Function`/`document.write`, each
  verified against a canary. Registry coverage of JS/TS taint flow is still
  thinner than the Python equivalent. ShellCheck covers the shell scripts
  (7 files, clean).
- **The build path now depends on `codeload.github.com` being up.** New, and
  a consequence of the work above rather than a defect in it. Every action is
  pinned to a commit SHA, which guarantees the *right* bytes and says nothing
  about getting them *at all* — and the signing, SBOM, and attestation steps
  each add another tarball to fetch over that CDN. During GitHub's incident on
  17 August, `anchore/sbom-action` and `astral-sh/setup-uv` both failed to
  download with 429/502/503 after three retries, failing `publish` and
  `postgres` on commits that could not have broken either; reruns were green.
  The fix is emphatically **not** to unpin, which would trade the integrity
  property for an availability one. The options worth costing are vendoring
  the two or three actions that matter into the repository, or replacing
  `download-syft` with a digest-pinned syft container image so the fetch goes
  to a registry rather than to codeload. Until then, a red supply-chain job
  deserves a look at *which* step failed before anyone concludes the change
  broke something.

<a id="security-findings-this-deployment-absorbs"></a>
## Security findings this deployment absorbs

Raised by the external review on 18 August, checked against the code rather
than taken on trust, and then deliberately not fixed. Each is real. What
holds them open is the shape of the deployment — one instance, three
allow-listed operators, an operator-curated catalogue with no route that
adds a feed — and not a judgement that the code is right.

They are recorded here rather than in [WORKLOG.md](WORKLOG.md) alone for two
reasons. A finding that lives only in a worklog entry gets re-reported by the
next review, which costs the reviewer's time and the reader's confidence.
And the assumption each one rests on is invisible from the code: nothing in
`app/` says "this is fine because there are three users". **This is the
section to re-read before adding a fourth operator, a second instance, or a
route that lets a user add a source** — most of what follows changes
severity at that moment, and the first item does not depend on the operator
count at all.

- **The application connects to PostgreSQL as a superuser.** The one item
  here whose severity the operator count does not cap. `sre-tab-db.container`
  sets `POSTGRES_USER=sretab`, which the official image creates as the
  cluster superuser, and the application, the migration unit, and the backup
  all share the single `DATABASE_URL` secret. An application-level SQL
  injection therefore does not stop at reading the tables: `COPY … PROGRAM`
  is available to a superuser, and it executes commands.

  **Where those commands run is the part worth stating precisely, because
  the first draft of this entry got it wrong and said "the database host".**
  `COPY … PROGRAM` runs under the postmaster, and the postmaster here is
  uid 999 inside `sre-tab-db.container`, which is `ReadOnly=true`, has
  `DropCapability=all` with five capabilities added back for the
  entrypoint's chown, publishes no port, and is reachable only from
  `sre-tab.network`. So the blast radius is the database container and what
  it can reach — the data volume, the three tmpfs mounts, and the internal
  network — and reaching the host needs a container escape this deployment
  does not hand anyone. One nuance in the other direction, recorded as an
  open question rather than an answer: `NoNewPrivileges` is deliberately
  unset on this unit alone, for the AppArmor reason documented at length in
  the file, so whether uid 999 can regain root *inside* the container has
  not been tested. Even container-root holds only those five capabilities
  against a read-only rootfs.

  That is a smaller finding than "command execution on the host" and a
  larger one than nothing: an attacker who reaches it reads and writes every
  row, including sessions, and gets a foothold on the internal network. And
  nothing currently reachable gets there at all — the query layer is
  SQLAlchemy Core throughout with no string-built SQL — so this is a
  severity multiplier on a bug that does not exist yet rather than a live
  hole.

  Closing it means at least three roles rather than one: DDL for
  `alembic upgrade`, DML for the application, and read for the dump. That
  touches the migration unit, `restore.sh`'s ownership handling, and
  `smoke.sh`, which is why it is filed rather than fixed, and why it should
  be done deliberately rather than squeezed into an unrelated change.

  **The roles now exist; nothing uses them yet, deliberately.**
  `deploy/roles.sql` defines `sretab_migrate` (DDL), `sretab_app` (DML),
  and `sretab_readonly` (the dump), all `NOSUPERUSER NOCREATEDB
  NOCREATEROLE NOREPLICATION NOBYPASSRLS`;
  `deploy/scripts/create-roles.sh` installs them and is deliberately *not*
  called by `install.sh`. No `.container` file and no `DATABASE_URL`
  changed, so the running deployment is exactly as it was. The cutover is
  its own iteration, and `deploy/ROLES.md` carries the procedure, the full
  list of consumers, and the rollback.

  Verified against a real `postgres:18-trixie` rather than reasoned about,
  because the whole entry turns on one claim. `COPY … TO PROGRAM` is
  refused for all three roles — including `sretab_migrate`, so DDL rights
  do not imply it — and `sretab_app` cannot `CREATE TABLE`.

  Two details that would have been discovered the hard way. `pg_dump`
  needs `SELECT` on **sequences**, not only on tables: a control role
  granted table-`SELECT` alone made `pg_dump` fail outright, so a
  tables-only read role produces no backup rather than a subtly wrong one.
  And `ALTER DEFAULT PRIVILEGES` is written `FOR ROLE sretab_migrate`,
  because default privileges attach to the role that *creates* an object,
  not to whoever runs the `ALTER` — get that wrong and every future
  `alembic upgrade` yields tables the application cannot read, with
  nothing to say why.

  **Both sub-decisions the cutover was waiting on are now closed, and the
  units are still what is open.** `restore.sh` does `DROP`/`CREATE DATABASE`,
  which is database-level admin none of the three roles holds; it now keeps
  the superuser credential for exactly that step and runs `pg_restore` as
  `sretab_migrate`, rather than granting the DDL role `CREATEDB` and widening
  what the migration unit carries unattended on every deploy. And `smoke.sh`,
  which exercised only the superuser and would therefore have kept passing
  through a broken or silently reverted cutover, now installs the roles,
  runs migrate, app, session sweep, and backup as them, and asserts the
  refusals by their error text — `COPY … TO PROGRAM` for all three,
  `CREATE TABLE` for `sretab_app`, `INSERT` for `sretab_readonly` — so
  widening a role by accident fails CI instead of leaving ROLES.md quietly
  wrong.

  What remains is the cutover itself: the `Secret=` and `Environment=` lines
  in `deploy/quadlet/`, as its own commit, with the rollback in ROLES.md.

  The production-readiness review of 1 September 2026 put the cutover first
  among everything it found, on the ground this entry opens with: it is the
  only item in this section whose severity three operators do not cap.
- **The OAuth state cookie can be overwritten from a sibling subdomain.**
  `set_state_cookie` scopes to `/api/v1/auth` with `HttpOnly`, `SameSite=Lax`,
  and `Secure`, which is careful about everything except *which host* may
  write it: any subdomain sharing the registrable domain can set a cookie
  the browser will send here, so a cookie-toss overwrites the state and
  turns the flow into login CSRF — the victim ends the flow authenticated
  as the attacker. The exploit therefore needs the attacker's own GitHub ID
  on `ALLOWED_GITHUB_IDS`, which at three operators means it needs an
  operator, and an operator has better options. The fix is not free either:
  `__Host-` is the prefix that buys domain integrity, and it mandates
  `Path=/`, so taking it means giving up the path scoping that is currently
  doing real work. Worth revisiting if the domain ever hosts anything else,
  because that is the condition — a sibling subdomain existing at all — and
  not the user count.
- **Feed image URLs are validated more weakly than item URLs** — **landed,
  and closing it turned up a live hole in the *item* path.** The two
  functions now share one host rule (`_feed_url_host`), so they can no
  longer disagree: the image path applies the same control-character,
  credential, dotless-host, and IP-literal checks, and still returns `None`
  rather than raising, because an unusable decorative image must not fail
  the ingest of an otherwise-good item.

  The find was in `_looks_like_ip`, which tested `all(part.isdigit() ...)`.
  `"0x7f".isdigit()` is `False`, so `https://0x7f.0.0.1/...` was not
  recognised as a literal — and that check guards `normalise_item_url` too,
  so a **canonical URL** pointing at the reader's own loopback was being
  accepted and rendered as a link. That is a wider defect than the
  asymmetry this entry was filed for, and it was reachable without the
  asymmetry at all.

  Rather than write a third decoder, the check now calls
  `urlguard.parse_numeric_ipv4` — renamed from its private form, since a
  leading underscore crossing a module boundary is an undocumented contract
  that invites a fourth copy. One body, three test files: breaking the hex
  branch now fails `test_urlguard.py`, `test_fetch.py`, and
  `test_normalise.py` together, which is what makes the sharing real rather
  than nominal.

  The original entry follows, because it is what made the fix cheap.

- **Feed image URLs are validated more weakly than item URLs.**
  `normalise_item_url` rejects control characters, credentials, a host with
  no dot, and an IP literal, and raises on anything it will not take.
  `_safe_optional_url`, which handles `image_url`, checks the scheme, the
  presence of a host, userinfo, and the length — and stops. So an image
  host may be an IP literal or a bare name that an item URL could not be,
  `img-src 'self' https: data:` in [middleware.py](app/middleware.py) lets
  the browser fetch it, and the browser makes that request from the
  operator's network rather than from the server. Three things blunt it:
  sources are added through the CLI by an operator and there is no route
  that adds a feed, the request is blind in that nothing reads the response,
  and `referrerPolicy="no-referrer"` means the fetch carries nothing about
  where it came from. The asymmetry is still not defensible on its own
  terms — the two functions disagree about what a URL from a feed is
  allowed to be — and the cheap close is to give the image path the same
  host rules and keep returning `None` instead of raising.
- **`user_sessions` rows are never deleted** — **landed.** `sre-tab sessions
  prune` sweeps the table and `sre-tab-prune-sessions.timer` runs it daily
  at 04:17 UTC — after the backup's jitter window closes at 03:42, so a
  `pg_dump` snapshot never races the `DELETE`, and a swept row survives in
  the latest dump for a further day.

  Two classes of dead row, dead at different moments. Expired-and-never-
  revoked goes immediately; it records only that a session ran out. Revoked
  is held seven days, because `revoked_at` is the sole trace this system
  keeps that a logout or a token rotation happened, and the week that
  matters is the week after a suspected compromise. Retaining it grants
  nothing — `resolve_session` refuses on `revoked_at IS NULL` either way —
  and seven days is shorter than the default `session_ttl_days` of 14, so
  the window never holds a row longer than doing nothing would have. A row
  that is both revoked and expired is held by the grace window rather than
  swept by the expiry branch.

  The original entry understated the growth rate. Sign-in *rotates*: it
  revokes the previous session and inserts a new one, so rows accrue at the
  rate users open the application, not the rate they remember to log out.

  Filed against the entry below, which stands as written otherwise.

- **`user_sessions` rows are never deleted.** `create_session` inserts,
  logout sets `revoked_at`, and reads filter on `expires_at`; nothing
  removes an expired or revoked row, so the table grows by one row per
  sign-in forever. `prune_feed_items` exists and runs on a schedule, so the
  place to put the equivalent is already built. Negligible at three
  operators — tens of rows a year — which is the only reason it is not
  done.
- **`upsert_user` is select-then-insert with no conflict handling.** Two
  concurrent first sign-ins for the same GitHub ID both find no row and
  both insert. `users.github_id` is `unique=True`, so the loser gets an
  `IntegrityError` rather than a duplicate — the integrity of the table is
  never at risk, and the symptom is a 500 on one of the two callbacks and a
  retry that then succeeds. It needs the same person signing in twice at
  once, on their very first sign-in, which is a narrow enough window that
  it is filed rather than fixed. `ON CONFLICT DO UPDATE` would close it,
  at the price of a dialect-specific statement in a function that is
  currently dialect-neutral.

  That price is already paid, once, somewhere else.
  [app/services/upsert.py](app/services/upsert.py) provides `insert_ignore`,
  an `INSERT … ON CONFLICT DO NOTHING` implemented for both dialects behind
  one signature — so the select-then-insert becomes an insert-ignore
  followed by a select on `github_id`, the loser of the race reads the
  winner's row instead of raising, and `upsert_user` stays as
  dialect-neutral as it is today. What holds this open is the narrowness of
  the window, not the size of the fix.

<a id="api-surface"></a>
## API surface

- **`docs_enabled` should default to `False`** — **landed.** A deployment that
  inherits only the defaults — a container run by hand, a second instance,
  anything not derived from `deploy/app.env.example` — no longer publishes an
  interactive client against its own API because nobody said not to.
  `.env.example` still sets `DOCS_ENABLED=true`, since that file is the
  development template. `/api/v1/openapi.json` is unaffected and served
  either way: the flag governs the UI, not the contract.
- **Serve a static OpenAPI document in production** — **the drift check has
  landed; the serving change has not, and the ownership problem was
  overstated.** Publishing the schema at `/api/v1/openapi.json` is a v1
  requirement and stays; the change is decoupling it from the running
  application so the served artefact is a reviewed, versioned file.

  This was parked on ownership rather than difficulty: `app/main.py` mounts
  no static files and is frozen Phase 0 property, the only place the served
  artefact could be decoupled from the live app is `deploy/Caddyfile`, and a
  committed artefact needs a drift check whose natural home is
  `frontend/openapi.json`. The cheap version turned out to touch neither of
  the contentious files, because a drift check is not a serving change: it is
  a property of the application, and it went in the test suite.

  It also had one more link than the entry accounted for. The contract
  reaches the client through *two* committed artefacts —
  `frontend/openapi.json`, and the `src/api/schema.d.ts` generated from it —
  and the regeneration of both was held together by a sentence in
  frontend/README.md asking for discipline. Both links are now checked, each
  in the job that already has the toolchain for it, so neither job gained a
  dependency and no new job was created: `tests/test_openapi.py` compares the
  committed document against the served schema byte for byte, and the
  `frontend` job regenerates the types and fails on a diff. Both were green
  on the first run and both were mutation-tested — a field added to
  `HealthResponse` with no regeneration fails the first, and a regenerated
  `openapi.json` with stale types fails the second.

  The interesting half is the one `tsc` could never have caught: stale types
  stay internally consistent with a document that has stopped describing the
  server, so the typecheck passes faithfully against the wrong contract.

  The `frontend` job was renamed to say so — `Frontend lint, types, contract,
  tests, build` — and the required context updated with it. Dodging the
  settings edit by keeping a name that no longer described the job was the
  first instinct and the wrong one: it optimises for whoever makes the change
  over whoever next reads a red cross, which is the same least-surprise
  argument the preceding three fixes turned on.

  What remains is the serving change itself — `deploy/Caddyfile` answering
  `/api/v1/openapi.json` from the committed file rather than from the
  application. That still wants an owner, and now has a trustworthy artefact
  to serve, which was the prerequisite it was really waiting on.

<a id="scaling"></a>
## Scaling

None of these bite at v1's target of 100 users and 25 sources; each is a
prerequisite for going past it.

- **Shared state store.** OAuth state and the rate limiters are process-global
  — correct for a single instance, wrong the moment there are two.
- **Separate scheduler worker.** The PRD already requires this before
  horizontal scaling. Per-source PostgreSQL advisory locks mean replicas
  never fetch the same source concurrently, but aggregate fetch frequency
  can still exceed `refresh_minutes` with N replicas.
- **Rate limiting keyed on a trusted client address.** Works today, but it
  depends on the `trusted_proxies` / `FORWARDED_ALLOW_IPS` pair staying in
  step; a shared store would let this move somewhere less fragile.

<a id="operations"></a>
## Operations

- **Off-host backups.** `/srv/sre-tab/backups` sits on the same host as the
  database. That is a backup, not disaster recovery. The integrity half is
  already built — `backup.sh` writes a `.sha256` sidecar beside every dump,
  under the same mask — so what is missing is a copy to another host and a
  verify at the far end, and not a backup format, a checksum scheme, or a
  restore procedure. All three of those exist and `smoke.sh` runs them on
  every push.
- **`OnFailure=` alert unit.** Deliberately not invented in v1 — orbit-data's
  equivalent is a subcommand of its own application, and sre-tab had no CLI
  at the time. It has one now, and `sre-tab status` already exits non-zero
  when an enabled source is failing, so the alert path has something to call.

  The shape that needs no new dependency: a timer running `sre-tab status`,
  and `OnFailure=` on that service pointing at whatever the host already
  uses to reach a person. What it buys is the part worth stating, because it
  is invisible from either piece on its own — a failing source is currently
  visible only if somebody runs the CLI. The readiness probe knows and
  deliberately does not say: `app/scheduler/service.py` returns `ok=True`
  with the failure count in the detail string, because one broken feed must
  not take the instance out of rotation. Readiness and alerting want
  opposite answers to the same question, and only one of them is being
  asked.
- **Nothing here can be installed by version.** `v1.0.0` is a git tag and
  nothing more: no Release object against it, so the tag carries no notes
  and none of the artefacts the build already produces, the SBOM among them.
  `CHANGELOG.md` has accumulated an `[Unreleased]` section
  substantially larger than the release it sits above. And the registry
  holds only `sha-<commit>` tags, because the publish job runs on pushes to
  `main` and on nothing else, so the only path to a known-good deployment is
  `promote.sh` run from a checkout of this repository. That is exactly right
  for the reference host and useless to anyone else: there is no version to
  ask for.

  What closes it is one iteration rather than one change — cut 1.1.0, create
  the Release with notes and the SBOM attached, and add a tag-triggered
  publish that pushes `:1.1.0` and `:1.1` alongside the digest. The
  digest-pinned promotion stays exactly as it is, for the reason
  [Supply-chain hygiene](#supply-chain-hygiene) gives: a moving tag decides
  the running version by whoever pushed last, which is the property that
  entry exists to have removed. A floating `:1.1` is a convenience for
  people not running these quadlets, and it is not what the units point at.
- **Frontend unit tests** — **landed, and they found things.** Vitest, 114
  tests in three files, no jsdom: the theme tests install by hand the two or
  three globals the theme layer touches, so a new global dependency shows up
  as a failure rather than being supplied silently. They cover theme
  resolution and its `localStorage` fallbacks; the anti-flash script
  `public/theme-init.js` executed in a `node:vm` context across every stored
  value × OS preference combination, which is the first thing to check that it
  agrees with the module it necessarily duplicates; and `tokens.css` parsed so
  WCAG contrast ratios are recomputed for every text and boundary pair in both
  themes. Dark mode had never been independently verified and was not clean:
  button, input, and inactive-chip borders sat at 1.80:1 against 1.4.11's 3:1,
  and read-card summary text at 3.22:1 in light against 1.4.3's 4.5:1. All
  fixed at the token layer.
- **Run the frontend tests in CI** — **landed.** The `frontend` job runs
  `npm test` between the typecheck and the build: the suite is pure logic with
  no build dependency and finishes in well under a second, so failing early
  costs nothing.
- **Widen frontend coverage beyond the theme layer** — **the cheap half has
  landed; the expensive half has not.** The suite was thorough about theme
  resolution, the anti-flash script, and contrast, and covered none of the
  client's actual logic.

  `src/feed/filters.ts` and `src/feed/volume.ts` now have 73 tests, and they
  needed no new tooling — both modules import types only, so they are the same
  shape as what Vitest already covered. They were mutation-tested rather than
  merely run: thirteen behavioural mutations, each the plausible version of
  the mistake, and all thirteen fail the suite. `filters.ts` was the higher
  value of the two because it encodes a distinction that breaks silently —
  `null` means "no override, use my saved selection" and `[]` means "the user
  deselected everything, so nothing can match and the request is skipped" —
  and the tests pin the URL round trip, which is where that distinction has to
  survive between renders.

  Two limitations were first written up as documented assumptions, on the
  premise that slugs are kebab-case and so could not contain the delimiters
  either function uses. **That premise was asserted rather than checked, and
  it is false** — see the slug-format item below. Both were therefore
  reachable defects, and review caught it. What changed as a result:
  `filterKey` now encodes as JSON instead of joining on `*` and `+`, so no
  slug can alias one selection onto another's cache entry; and the comma case
  is marked `it.fails` with the behaviour we want, so it records the gap
  without pinning the defect as correct and errors the day someone closes it.

  `usePagedResource` and `src/api/client.ts` are the expensive half and are
  still untested: hooks and `fetch` mean a DOM environment and request
  mocking, which is real setup and probably a dependency or two. Still worth
  doing, and still not the thing to pick up first.

- **Nothing constrains a slug's format at any creation path** — **landed.**
  Resolved towards enforcement, on least-surprise grounds: the surprise here
  belongs to the operator at a terminal, and for a CLI that means failing at
  the point of the mistake rather than three components downstream.
  `add_source` and `add_topic` now refuse anything that is not lower-case
  alphanumerics joined by single hyphens, within the column's 64 characters,
  reusing the pattern that already guarded the Medium tag. `sre-tab status`
  reports rows that predate the check and exits non-zero, because
  enforcement at add time binds only what is added after it and a slug cannot
  be rewritten in place without breaking every saved selection naming it.

  One dialect divergence fell out of writing it down: `medium_source` capped
  the *tag* at 64 characters and then prefixed `medium-`, so a 60-character
  tag made a 67-character slug — accepted by SQLite in development and
  refused by PostgreSQL in production. The composed slug is now checked too.

  The client was deliberately left as it is. Its fragility is contained
  rather than fixed, which is why the `it.fails` marker stays: the constraint
  now lives somewhere else, and a reader should be able to find that out.

  The original entry follows, because the reasoning is what made the choice.

- **Nothing constrains a slug's format at any creation path.** The catalogue
  is operator-seeded and every slug in it is kebab-case, which is why this
  read as a rule and got asserted as one. It is not enforced anywhere:
  `add_source` checks uniqueness, the feed URL, and the refresh interval;
  `add_topic` checks uniqueness alone; and `sources.slug`/`topics.slug` are
  plain `String(64)` with no CHECK constraint. The one strict slug pattern in
  the tree guards the Medium *tag* expansion (`app/cli/catalogue.py`), because
  that value is interpolated into a feed URL — it says nothing about the
  general `add-source` and `add-topic` paths.

  A slug is not an inert label. It goes in the URL, in a cache key, and in the
  query the server builds, so its shape is load-bearing in three places that
  each assume something different. The comma case above is the live
  consequence: `sre-tab source add --slug 'a,b'` produces a source the feed
  cannot filter to.

  Two ways to close it, and they are not equivalent:

  Enforcing a slug pattern at every creation path is the smaller change and
  matches how `validate_feed_url` was handled — make the invalid state
  unrepresentable rather than teach each consumer to cope. It does not repair
  slugs already stored, so it wants a check over the existing catalogue too.

  Making the frontend preserve arbitrary slugs — repeated query parameters
  (`?sources=a&sources=b`) instead of a comma-joined value — is also small and
  is robust to whatever the database actually holds. Its cost is that the
  browser URL format changes, so any bookmarked filter URL in the old format
  reads back as one slug rather than several. On a three-operator deployment
  that is close to free, but it is a user-visible change and should be a
  decision rather than a side effect. The wire format is unaffected either
  way: `fetchFeed` sends arrays through openapi-fetch, so this is purely the
  browser URL.

- **"Save as my default" inverts an empty selection** — **landed.** Found by
  the tests above, then decided rather than merely patched. `saveAsDefault`
  wrote `effectiveSelection`'s result straight into preferences, moving `[]`
  from the override side of the distinction to the saved side, where it means
  the opposite: the user's "show me nothing" was stored as "show me
  everything", two clicks from the feed, with no error. Worse than it sounds
  — it landed the user in precisely the state
  [preferences.py](app/services/preferences.py)'s default-selection rationale
  exists to prevent, where general news drowns the low-volume sources the
  product is for.

  Resolved by treating an empty selection as what it is: **a step, not a
  destination.** It exists so you can clear the chips and pick two rather
  than deselecting sixteen. The store has no way to say "my default is
  nothing" — an empty saved selection is how the server spells "no
  preference" — so the control no longer offers to save it, and says why in
  the filter bar rather than presenting a dead button. Making the state
  representable was considered and rejected: it is a schema change and a
  server-logic ripple to persist a state whose only value is transient.

  A second fault fell out of the same root, and is fixed with it.
  `saveAsDefault` wrote *both* dimensions from `effective`, which resolves an
  un-overridden dimension into the whole catalogue for rendering. Writing
  that back converts "follow the instance" into a pinned snapshot of today's
  catalogue, after which a source added later never appears for that user and
  nothing indicates why. It only bites once saved preferences are empty —
  which is exactly what the inversion above caused, so the two compounded.
  The patch now carries only the dimensions the user actually overrode;
  `PreferencesPatch` already treats an absent field as "leave alone".

  The general lesson is worth more than the fix: `effectiveSelection` returns
  a **display** value, lossy by design because it resolves *unset* into a
  concrete list so chips can render. Persisting a display value into a store
  with a different vocabulary is what inverted the meaning. Persist intent,
  not appearance.

<a id="things-that-are-true-but-unproven"></a>
## Things that are true but unproven

Not deferred work so much as deferred *evidence*. Each is believed correct
and has not been demonstrated, and saying so is cheaper than discovering it
during an incident.

- **The backup timer's `Persistent=true` catch-up.** Demonstrating it needs
  the host down across 03:22 UTC and then brought back. The backup *script* is
  well covered without it: `deploy/scripts/smoke.sh` runs the real `backup.sh`
  and the real `restore.sh` on every push, and asserts the dump, its `.sha256`
  sidecar, and a restore that brings back both a marker row and the Alembic
  revision. What no automated run covers is the scheduling around it — the
  timer firing on its own, and the catch-up after downtime.
- **The fetcher's accept-a-redirect branch, against a live server.** An
  https → https hop is followed with the destination re-validated and
  re-pinned in its own right, and that has unit coverage against a mocked
  transport including a relative `Location`. It has no real-world provenance:
  none of the nineteen candidate feeds surveyed for the catalogue redirects at
  all. The refusal branches are the ones with a real example behind them —
  `https://www.theguardian.com/uk/rss/` answers `301` to `http://`, which is
  where that whole class of trap was found.
- **Quadlet runtime behaviour beyond one Linux pass** — **a second host has
  now run it, and `Notify=healthy` was doing something nobody had measured.**
  CI machine-checks unit *generation* with `podman-system-generator --dryrun`,
  which catches a malformed key and nothing else.

  A full pass on a second Debian 13 host with podman 5.4.2 confirmed the
  ordering (`db` → `migrate` → `app` → `web`, in that order, from a cold
  install), the Podman secret plumbing, and `systemctl --failed` staying empty
  across stops — which is the `NoNewPrivileges` reasoning holding up somewhere
  other than where it was written.

  What it also found is that `Notify=healthy` was setting the deploy window,
  not merely gating readiness. The application's healthcheck lived in the
  image at a 30-second interval while the database's lived in its unit at 10;
  the first check runs one whole interval after start, so systemd held Caddy —
  ordered after the application — down for the entire wait. A documented
  "sub-second blip" measured 43.7 seconds on an application that was answering
  2.8 seconds in. The image is 10s now, which takes the `systemctl` wait from
  35.6s to 15.4s. See the deploy-window table in
  [deploy/README.md](deploy/README.md).

  Still not a soak: this is two cold installs and a handful of restarts, not
  weeks of uptime, and the backup timer's catch-up is still unproven above.

- **~20s of the deploy outage happens after `systemctl` returns, and nothing
  explains it yet.** New, and separated from the healthcheck item above
  because fixing that one did not touch this and in fact made it the majority
  of the window: total unreachability moved only 43.7s → 36.1s.

  What is known. During the tail a TCP connect to the published port is
  **black-holed rather than refused**, so it is not "no listener yet". Caddy's
  own log has it serving 50ms after its container starts, so it is not Caddy
  booting. The application is answering 2.8s in, so it is not that either.
  Something between the published port and the container is not carrying
  traffic, and the shape — silent drops after a container is recreated — points
  at the NAT layer rather than at any of the three services.

  The obvious next measurement turned out to be malformed, which is worth
  recording so nobody spends the afternoon on it. The observations came from
  the host's own loopback through podman's DNAT, and the first write-up
  hedged that an off-host client might not see the tail at all. There are no
  off-host clients: `sre-tab-web.container` publishes `127.0.0.1:8080:8080`
  deliberately, because TLS is terminated by the host's existing proxy, and
  that proxy reaches Caddy over loopback exactly as the polling did. So the
  hedge was backwards — nothing insulates users from this, it reaches them as
  502s — and opening the port to test it would have measured a topology this
  deployment does not have.

  The mechanism is narrowed, and it is not conntrack, which was the first
  guess. Two things serve `127.0.0.1:8080`. netavark installs a hostport rule
  — `ip daddr 127.0.0.1 tcp dport 8080 dnat ip to 10.89.61.20:8080`, pointing
  at Caddy's pinned address — and podman separately holds a *reservation*
  listener on the same port, visible as `conmon` in `ss -lntp`, so nothing
  else can take it while the container is down.

  Probing across a restart walks through all three states in order: `refused`
  while both are gone, then **`accepted then hung`** — a connection the
  reservation listener takes and never forwards, because the DNAT rule is not
  back yet — then serving. The tail is that middle state. It looks like an
  outage with a listener present, which is why it reads as black-holing from
  the client side and why nothing in the three services' logs mentions it.

  Not yet established: whether the ordering is fixable from the unit files at
  all, or is podman's to fix. `PublishPort=127.0.0.1:8080:8080` is the same
  line orbit-data uses, so anything learned here applies there too.

- **`requires-python = ">=3.12"` is a floor CI stopped testing today.**
  The five setup-python steps now take `python-version-file:
  .python-version` — the fix for a 3.14 that was installed and then silently
  not used — and the consequence is that CI exercises one interpreter, 3.14,
  where it previously exercised one interpreter by accident. The floor is
  not a guess: the full suite was run by hand on 3.12.13 in a separate
  environment for that change. It is simply never run again, so the first
  3.13-or-later syntax or stdlib behaviour to reach `app/` will be published
  in metadata that still promises 3.12, and nothing will say so.

  This one has a decision attached rather than only a measurement: a matrix
  job on the floor, which costs a second run of the suite on every push and
  keeps the promise honest, or `requires-python = ">=3.14"`, which stops
  making it. Either is small. Drifting is the option that costs something,
  and it is the one that happens by default. The reasoning is in
  [WORKLOG.md](WORKLOG.md).
- **The Containerfile's setuid inventory was counted on a base image that is
  no longer the base image.** The comment above the strip names eleven
  setuid and setgid files — mount, umount, su, passwd, chsh, chfn, chage,
  expiry, gpasswd, newgrp, and unix_chkpwd — counted on
  `python:3.12-slim-trixie`. Both stages moved to `python:3.14-slim-trixie`
  and nobody has counted them there.

  Nothing load-bearing rests on the number. The `RUN` strips whatever it
  finds and then asserts the end state rather than the exit status of the
  traversal, and ci.yml re-asserts it against the built image, so the
  property survives a base image with a twelfth file or none at all. What
  drifts is the comment — and the comment is what a reader consults when
  deciding whether the strip is still doing anything, which is the question
  `sre-tab.container`'s missing `NoNewPrivileges=true` makes them ask. A
  one-line item, worth folding into the next base image move.

<a id="documentation"></a>
## Documentation

- **The README's quickstart is executed on every push** — **landed.**
  `.github/workflows/docs.yml` extracts the commands from `README.md` itself
  rather than copying them, so the workflow cannot pass while the document it
  protects has stopped being true. Two wrong procedures preceded it:
  `install.sh --start` never recreated a removed network, and the documented
  upgrade sequence was wrong as written.
- **`deploy/README.md` is executable now, and CI still cannot run it** —
  **half landed, and the original entry was right for the wrong reason.** It
  said the expensive part was a runner with systemd. `ubuntu-latest` has
  systemd, root, and podman a package away, so that looked wrong — and then
  the job failed anyway, on something narrower: Ubuntu's `conmon` is built
  without journald support, and the three long-running units set
  `LogDriver=journald` deliberately, so `sre-tab-web.service` dies with
  `conmon failed: exit status 1` before a single procedure is exercised.

  Making it pass would mean overriding `LogDriver` for CI, which tests a
  deployment other than the one that ships. A green gate over the wrong
  artefact is worse than no gate, so the job was removed and the reason left
  in `docs.yml` where the next person to try will find it. What would close
  this is a runner whose conmon has journald: a Debian-based self-hosted
  runner, or podman from a repository that ships one.

  The markers and the harness are real and stay. The procedures run end to end
  on Debian 13 with podman 5.4.2 — verified, exit 0 through all seven blocks —
  and [CONTRIBUTING.md](CONTRIBUTING.md) carries the command.

  The other half of the original cost was two documented commands that could
  not run as written — `sudoedit /etc/sre-tab/app.env`, and a client-secret
  path written as `/path/to/…`.

  Resolved by naming them rather than by scaffolding around them. The secret's
  path is `${GITHUB_CLIENT_SECRET_FILE:?}`, which is a variable holding a
  *path* and so keeps the document's own rule that argv never carries the
  secret; and the configuration section now documents the non-interactive edit
  alongside `sudoedit`, which is what a configuration-management run does
  anyway. Both are improvements to the document in their own right, which is
  the test of whether bending it toward execution was legitimate.

  Seven blocks run: preparation, configuration, secrets, first start,
  verification, network replacement, and an assertion that the recreated range
  starts above Caddy's pinned `.20`.

  The verification block changed as a consequence of the deploy-window
  measurement above: it polls for `healthz` instead of requesting it once,
  because `systemctl` returning is not the all-clear. A document that told an
  operator to run a single request was telling them to run a flaky check.

  What is still not executed: the upgrade sequence, which needs a second
  published build to promote to, and the backup and restore procedures, which
  `smoke.sh` already covers through the same scripts.

  The procedure is once-only per host by design and the harness inherits that:
  `create-secrets.sh` refuses to run against an existing database password, so
  a re-run on a host that has already been installed fails at that step. CI
  gets a clean runner for free. Anyone reproducing this on a real host has to
  remove the secrets, the volumes, and the containers first — removing only
  the secrets leaves a database whose password nobody holds, which is exactly
  what that guard exists to prevent.
- **Make `Docs` a required check** — **landed, as a side effect of fixing the
  branch-protection rule.** Both of its check-runs — `README quickstart runs
  on a clean checkout` and `Relative links resolve` — are in the required set
  of eight, listed in
  [CONTRIBUTING.md](CONTRIBUTING.md#branch-protection). The rewrite that
  corrected the job-key/display-name mistake replaced the context list
  wholesale, so this was picked up in the same write rather than as its own
  task.

<a id="repository"></a>
## Repository

Consequences of the repository being public that are decisions rather than
tasks.

- **No licence file** — **landed.** `pyproject.toml` declared
  `license = "MIT"` while the repository granted nothing, so the only
  statement of terms lived in packaging metadata a reader never sees — on a
  public repository that is an inconsistency rather than an omission, since
  the metadata claimed terms the repository did not offer. `LICENSE` (MIT,
  © 2026 Mike Preston) now makes the existing claim true.
- **Read the branch-protection rule, and correct it if it needs correcting** —
  **landed, and it had never enforced anything.** GitHub keys a required
  status check on the check-run **context**, which for Actions is the job's
  `name:` whenever one is set. The rule had been created with the job *keys*
  — `python`, `postgres`, `audit`, `frontend`, `container` — which no
  check-run here has ever reported. The read finally succeeded once GitHub's
  incident of 17 August 2026 eased and confirmed exactly that, matching what
  the creating `PUT` response had recorded.

  It failed safe: a pull request waits on a status that never arrives rather
  than merging unchecked. But the real checks were not required either, and
  nothing would ever have surfaced it — every commit on `main` is a direct
  push, there has never been a pull request here, and required checks are not
  consulted on that path. The required set is now the eight reported
  check-run names, verified by set-differencing them against the check-runs
  the repository actually reports rather than by reading the rule back.

  `Publish, sign, and attest image` is deliberately excluded: it never runs
  on a pull request, and whether requiring a job skipped that way blocks the
  request or quietly passes was untested here, so it was excluded on the
  asymmetry rather than on a known deadlock.

  The repository's first pull request (#4) then exercised the corrected rule
  on a real merge path rather than by set-difference. All eight required
  contexts reported and passed, and the request came back `MERGEABLE` — which
  is the property the set-difference could only infer. It also measured half
  the asymmetry away: the excluded job *does* report on a pull request, as
  `SKIPPED`, so it is not the never-reports case that leaves a request pending
  forever. Whether protection would accept that `skipped` as satisfying a
  required context is still unmeasured, and stays that way — it can only be
  tested by requiring the job, which is the risk the exclusion exists to
  avoid.

  Worth knowing for the next reader of a rollup: `mergeStateStatus` came back
  `UNSTABLE` rather than `CLEAN`, because a third-party reviewer (CodeRabbit)
  posts a check that is not in the required set. `UNSTABLE` means mergeable
  with a non-required check outstanding; it is not a protection failure.

  The lesson generalises past this instance. **A job rename is a
  branch-protection change**, and nothing in the repository can detect it,
  because protection lives in GitHub's settings rather than in a file anyone
  reviews. [CONTRIBUTING.md](CONTRIBUTING.md#branch-protection) carries the
  read and fix commands for the next time a job is renamed.

- **Issue and pull-request templates, and nothing that makes the repository
  findable.** Neither template exists, which was the whole of this entry and
  is now the smaller half of it. The repository also carries no topics, no
  homepage URL, and no social preview image — read from its own metadata on
  1 September 2026, not inferred from the tree. So it is discoverable by
  name, by someone who already knows the name.

  One item rather than four because they share a cost and a condition. Each
  is minutes of work; the templates matter only once contributions arrive,
  and the other three are most of what decides whether any do.

<a id="product"></a>
## Product

The first three are v1 scope deferrals and are specified in
[prd-v1.md](prd-v1.md). The rest came from the production-readiness review
of 1 September 2026, and they are open for one shared reason rather than
seven: v1 was built for three operators who each hold a shell on the
reference host, so nothing a shell could already do was built a second time.
That is the right v1 and a narrow product. Most of what follows is the
difference between the two.

- **Per-device preferences (v2).** Already specified in the PRD: rows keyed
  `(user_id, device_id)` holding only explicit overrides, merged over the
  account profile on read. The v1 schema keeps account preferences separate
  from sessions precisely so this stays cheap.
- **Non-RSS sources.** Hashnode needs sitemap parsing or GraphQL; anything
  else requiring a bespoke adapter follows the same rule. The fetcher rejects
  these at configuration time today rather than growing special cases.
- **Richer authorisation.** v1 is a static allow-list of GitHub numeric IDs
  behind a single seam, so org or team resolution can replace it without
  disturbing the OAuth flow around it.
- **There is no five-minute path to a running instance.** The README's
  quickstart is development mode — SQLite, two processes, `npm run dev`, no
  container — and [deploy/README.md](deploy/README.md) is a Debian 13 host
  with rootful podman, quadlets, and systemd secrets. Between the two sits
  the arrangement most people who self-host anything actually run, and this
  repository has nothing to say to it: a `compose.yaml` over the published
  image and the same [deploy/Caddyfile](deploy/Caddyfile).

  The composition is not new work so much as new packaging. `smoke.sh`
  already stands the full stack up — PostgreSQL, migrations, the
  application, Caddy — on every push, and it is engine-agnostic on purpose:
  CI runs it under podman and `CONTAINER_ENGINE=docker` is a documented,
  supported path for a developer machine. Judged on reach per hour spent,
  this is worth more than any feature under it.
- **A denied sign-in ends in a JSON 403 that the README has to apologise
  for.** An account not on the allow-list reaches
  [app/api/v1/auth.py](app/api/v1/auth.py), which raises `HTTPException` —
  so a browser that has just completed a GitHub authorisation is shown a
  bare `403` body, which reads as a broken OAuth application, which is why
  [README.md](README.md) spends a front-page section explaining that it is
  not one. Documentation is standing in for an error message.

  The mechanism to fix it is already there and already used by the
  neighbouring branch: a cancelled flow calls `_landing_redirect`, which
  returns the browser to the landing page carrying a fixed `?signin=`
  outcome the frontend maps to a message. The denied flow should do the
  same, with an outcome whose message names the user's own numeric GitHub ID
  — the exact value `ALLOWED_GITHUB_IDS` wants, public information either
  way, and known at the point of refusal because authorisation happens after
  the profile fetch and before any row is written. Two things stay as they
  are: the failure limiter is still hit, because a grinder reaches this
  branch as easily as a first-time operator does; and how the ID travels to
  the page is the one open design question, since everything in that
  redirect is a fixed token today and this would be the first value in it.
- **`is_admin` exists, and nothing sets it or reads it.** The column is on
  `users` with a `false` server default and `MeResponse` carries it to the
  client. That is the entire implementation. Adding an operator means
  editing `app.env` and restarting the unit; adding a source means a shell
  in the container. A small admin API behind that flag — sources add,
  enable, disable, and set-topics, plus topics and refresh status — with a
  page under Settings closes both, and the two expensive prerequisites are
  built: CSRF is structural on mutating routes, and the add-time URL check
  is `validate_feed_url`, which the CLI path already goes through.

  **Re-read
  [Security findings this deployment absorbs](#security-findings-this-deployment-absorbs)
  before writing any of it, as a checklist rather than as background.**
  Several entries there are held open on the stated condition that no route
  adds a feed. This is that route. Each of those findings changes severity
  the day it merges, and the route being admin-only is not a detail of the
  design but the thing that decides how far each one moves.
- **Ninety days of retained items and no way to search them.**
  `feed_retention_days` defaults to 90, so there is a real corpus behind the
  feed and the only way to reach anything in it is to scroll. The shape that
  fits what is already here is a `tsvector` over title and summary with a
  GIN index on PostgreSQL, and FTS5 or a `LIKE` on SQLite for development,
  applied as one more predicate inside the existing keyset query rather than
  as a second endpoint beside it. That is how the read-state filter went in
  and for the same reason: a predicate in the `WHERE` of the statement that
  already runs keeps pages full and cursors honest, where filtering the
  page after it comes back does neither.
- **The pitch is that the data lives on your own server, and there is no way
  to get it out.** `DELETE /api/v1/me` exists, so an account can be
  destroyed, and nothing exports it first. `GET /api/v1/me/export` returning
  preferences, bookmarks, and read state as JSON is the user's half. The
  operator's half is OPML, both directions, in the CLI: the catalogue is
  seeded by `sre-tab seed` or assembled one `source add` at a time, and an
  import would turn standing an instance up around somebody's existing
  reading into a single command.
- **Four things missing from the first screen, none of them deep.** Sources
  render an icon and no source has one:
  [ItemCard.tsx](frontend/src/components/ItemCard.tsx) returns early without
  an `icon_url`, and the seed catalogue sets it on nothing, so the
  affordance is built and never fires. There is no mark-all-read for the
  current filter, which is the first control a reader with an unread filter
  reaches for. Per-source freshness is operator-only — `sre-tab status`
  knows when each source last fetched and no API field carries it, so a user
  cannot tell a quiet source from a broken one. And
  [app.css](frontend/src/styles/app.css) holds exactly one media query, at
  42rem, which makes the chip bar on a phone unverified rather than
  known-bad.
- **A `/metrics` endpoint.** Prometheus exposition is among the commoner
  self-hosting asks, and it costs this project nothing it has promised:
  a scrape is a local pull, so "nothing phones home" survives it intact.
  It ranks below the `OnFailure=` alert unit in
  [Operations](#operations), which closes the same loop for the reference
  deployment without a new dependency, a new route, or a scraper to run.
