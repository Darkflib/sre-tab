# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A tag-triggered publish path, so there is a version to ask for.** Until
  now the registry only ever received `sha-<commit>` and `latest`, because
  `publish` ran on pushes to `main` and nothing else — which is exactly right
  for the reference host and useless to anybody else. `ci.yml` now also runs
  on a `v*` tag, through the identical `needs:` chain, and a tag build
  publishes `:1.1.0` and `:1.1` alongside the commit tag and creates the
  GitHub Release with that version's changelog section and the SBOM the job
  already generates.

  Three decisions are worth stating because each could plausibly have gone
  the other way. **A tag build does not move `:latest`**, which stays the tip
  of `main`: a moving tag decides the running version by whoever pushed last,
  and that is the property the digest pins exist to have removed — a release
  moving `latest` would hand it back, and in the least expected direction.
  **A pre-release does not move the floating `:1.1`**, because `1.1.0-rc1`
  sorts below `1.1.0` and somebody asking for the stable minor line has not
  asked for a release candidate; `v1.1.0-rc1` publishes its exact version and
  nothing else. And **a tag with no `CHANGELOG.md` section fails the job**
  rather than producing a Release with an empty body, which would be a green
  check that verified nothing.

  The tag parsing, the version-tag rule, and the changelog extraction live in
  `.github/scripts/release-metadata.py` rather than in YAML, and
  `tests/test_release_metadata.py` drives them through the refusals —
  `v1.1`, `1.1.0`, `vfoo`, `v01.1.0`, `v1.1.0+build`, and a version the
  changelog does not mention — as well as the acceptances. Each guard was
  broken on purpose and seen to go red before being believed.
- **Off-host backups, verified at the far end.**
  `deploy/scripts/backup-offsite.sh` copies the newest dump and its `.sha256`
  sidecar to an `ssh://` target, an `s3://` target, or both — one
  space-separated list of URLs, where the scheme picks the transport, so an
  ssh mode configured with an S3 address cannot be written down. The copy is
  not the point: an upload exiting zero says nothing about the bytes at the
  far end, so ssh re-derives the checksum there and S3 is asked what SHA-256
  it recorded against the stored object (`x-amz-checksum-sha256`, never the
  ETag, which is an MD5 of part MD5s for a multipart upload and would quietly
  stop meaning anything once the dump got big). Proven by corrupting things:
  one flipped byte at the ssh far end and a truncated object in the store are
  each rejected non-zero.

  Caused by a successful backup rather than scheduled beside one — a drop-in
  adds `OnSuccess=` to `sre-tab-backup.service`, so it runs when a backup has
  just succeeded and does not run when one has just failed. `Requisite=` is
  the obvious spelling and is wrong: a oneshot without `RemainAfterExit` is
  inactive the instant it succeeds, so that gate never fires at all. Off
  unless `/etc/sre-tab/backup-offsite.env` exists, and loud once it does,
  including `OnFailure=sre-tab-alert@%n.service`.

  Neither transport gets a credential that can destroy what it has already
  sent. The ssh far end runs a forced command with four verbs and no delete,
  refuses to overwrite a published name, and does its own retention only
  after verifying the new dump; the documented IAM policy grants `PutObject`
  and `GetObject` on one prefix, with bucket versioning and Object Lock in
  compliance mode as the recommendation and a lifecycle rule as the retention
  mechanism. Requests are signed with `curl` and `openssl` rather than the
  AWS CLI, which on Debian 13 is 23 packages and 144MB and spools to a `/tmp`
  that `PrivateTmp=true` discards.

  The one uncontainerised unit in the deployment, and it pays for that:
  a dedicated `sre-tab-offsite` user, `ProtectSystem=strict`, an empty
  `CapabilityBoundingSet=`, and `systemd-analyze security` at 1.5. It reads
  the `0700` backup directory through a POSIX ACL the installer grants —
  rather than `CAP_DAC_READ_SEARCH`, which would have given it every file on
  the host in order to read two.
- `SECURITY.md`: a private reporting channel (GitHub security advisories),
  the supported-version table, and a pointer to the accepted findings in
  ROADMAP.md so a reporter can tell a new finding from a held one.
- Coverage is now a gate. `fail_under = 90` in `pyproject.toml` and
  `--cov=app` on the `python` job's pytest step, where the tooling was
  configured and nothing ever ran it. The threshold is a floor to catch a
  slide, not a target: it was set under the 94.23% measured when the gate
  went in, and the work in this release since took that to 94.44%. Proven
  to bite before being believed — at a temporary 96% the job exits 1.
- A read-state filter on the feed. `GET /api/v1/feed` takes
  `read_state=all|unread|read` (default `all`, so an existing client sees
  no change), applied as a predicate on the `user_read_items` join the
  query already carried — no schema change, and in the `WHERE` of the same
  statement so keyset pages stay full. The client carries it in the URL
  and in the pagination cache key, and the filter bar gains chips for it.
- Keyboard navigation on the feed and bookmarks: `j`/`k` to move, `o` or
  Enter to open, `m` to toggle read, `b` to bookmark, and `?` for a help
  overlay. Focus is a real roving `tabindex` with `.focus()` called, not a
  CSS-only selection, so the browser scrolls, screen readers announce, and
  `:focus-visible` works. Shortcuts never fire while typing in a field or
  when a Ctrl/Cmd/Alt modifier is held — `shiftKey` is deliberately not in
  that set, because `?` is Shift+/ on most layouts.
- `sre-tab sessions prune`, and `sre-tab-prune-sessions.timer` running it
  daily at 04:17 UTC. Nothing had ever deleted from `sessions`, so it grew
  by a row per sign-in forever — and faster than that sounds, because
  sign-in rotates: it revokes the previous session and inserts a new one.
  Expired-and-never-revoked rows go immediately; revoked rows are held
  seven days, because `revoked_at` is the only trace that a logout or a
  rotation happened and the week it matters is the week after a suspected
  compromise. 04:17 is after the backup's jitter window closes at 03:42,
  so a `pg_dump` never races the `DELETE`.
- **A failing source now reaches a person.** `sre-tab-status.timer` runs
  `sre-tab status --failures-over 3` hourly at :48, and
  `sre-tab-status.service` carries `OnFailure=sre-tab-alert@%n.service` —
  a template that gathers the failed unit's journal and hands it to
  `/etc/sre-tab/alert.sh`. Until now a broken feed was visible only if
  somebody ran the CLI: `/api/v1/healthz` knows and deliberately will not
  say, because `app/scheduler/service.py` reports `ok=true` with the failure
  count in its detail string so one dead feed cannot take the instance out of
  rotation. Readiness and alerting want opposite answers to the same
  question and only one of them was being asked.

  **The transport is the operator's and this repository does not ship one.**
  Reaching a person is a property of the host, and a mail client or an HTTP
  library would be a supply-chain decision taken on their behalf. `install.sh`
  installs `alert.sh.example` — msmtp and a `curl` webhook, both worked
  through — and warns at the end of every run while `/etc/sre-tab/alert.sh`
  is absent. That case is deliberately the loudest path in the whole chain,
  because an alert that goes nowhere quietly is the exact defect this change
  exists to remove: the report still reaches the journal, and
  `alert-dispatch.sh` exits 1 so the alert unit lands in
  `systemctl --failed` naming the file it wanted.

  Proven on the reference deployment rather than asserted — Debian 13, podman
  5.4.2, systemd 257: the Quadlet generator accepts the unit, the timer
  schedules, `OnFailure=` fires with the failed unit's name substituted, the
  journal reaches a stub transport, and the exit code moves between three and
  four consecutive failures. Two things that only turn up that way: `%n` (not
  `%N`) is what makes the template's `%i` a name `journalctl -u` accepts
  unmodified, and `systemd-run` does not expand specifiers in `--property=`
  at all, so the by-hand test in `deploy/README.md` names the instance in
  full.
- `sre-tab status --failures-over N`, defaulting to 0 so nothing changes for
  anyone running it by hand. It is strictly over: `--failures-over 3` clears a
  source on its third consecutive failure and fails on its fourth, which at
  the default 30-minute refresh interval is roughly two hours without a
  successful fetch. The unthresholded command exits 1 on a single failure,
  and on an hourly timer that pages a human for one transient 502 from one
  feed — an alert that fires on noise is an alert somebody mutes.

  It gates the refresh-failure half only. A malformed slug fails the command
  at any threshold, because the counter the threshold measures is one a
  malformed slug never increments — the source fetches perfectly and simply
  cannot be filtered to — so any value above zero would suppress a permanent
  configuration defect for ever. The cost is that it alerts hourly until the
  slug is re-added, which `deploy/README.md` states outright rather than
  leaving to be found at 03:00.
- Three least-privilege PostgreSQL roles in `deploy/roles.sql` —
  `sretab_migrate` (DDL), `sretab_app` (DML), `sretab_readonly` (the dump)
  — installed by `deploy/scripts/create-roles.sh`, with the reasoning, the
  full consumer list, and the rollback in `deploy/ROLES.md`. They landed
  ahead of anything using them, deliberately, so that the commit which
  switched the units over could touch nothing but `deploy/quadlet/` and be
  revertible on its own; see **Security**, below, for that step. Verified
  against a real `postgres:18-trixie`: `COPY … TO PROGRAM` is refused for
  all three, including the DDL role, which is the mechanism the finding
  turns on.
- **The deployment smoke test now runs as the three least-privilege roles
  and asserts what they may not do.** It installs `roles.sql` against its
  own throwaway PostgreSQL before the migrations, then runs the migration
  container as `sretab_migrate`, the application and `sre-tab sessions
  prune` as `sretab_app`, and the backup as `sretab_readonly` — each with
  its own password, so a container handed the wrong `DATABASE_URL` fails
  instead of connecting anyway. The negative assertions that `deploy/
  ROLES.md` had only made by hand are gates now: no role can `COPY … TO
  PROGRAM` (the mechanism the whole finding turns on, asserted for the DDL
  role too), `sretab_app` cannot `CREATE TABLE`, `sretab_readonly` cannot
  `INSERT`. Each matches on the *text* of the refusal, because a `psql`
  that fails from a typo or an unmade connection would otherwise read as a
  passing negative assertion. Every one was watched failing first: granting
  `sretab_app` `CREATE` on schema `public`, or membership of
  `pg_execute_server_program`, trips the assertion it should. So does
  naming the wrong role in `ALTER DEFAULT PRIVILEGES FOR ROLE` — which
  applies without error and then silently never fires, and is now caught by
  a table `sretab_migrate` creates having to be immediately usable by the
  other two.
- An open-work index at the top of `ROADMAP.md`. The file keeps landed
  items on purpose, which left no way to see what was still open without
  reading all 39KB of it.
- 65 Vitest tests over `src/api/client.ts` and the effects half of
  `src/data/usePagedResource.ts`, taking the client suite to 458. These are
  the two modules the roadmap called the expensive half: the fetch layer,
  and the part of the pagination hook that only exists once a component is
  mounted. The client tests pin the same-origin request the module builds,
  the CSRF header on mutating methods and its absence on safe ones, the
  401 broadcast that drops the session, and — the distinction every caller
  branches on — an HTTP error carrying its status against a network failure
  flattened to `status: 0`. The hook tests pin the initial load, cursor
  pagination, that a filter change discards the previous filter's pages
  rather than appending to them, and that a response from a superseded
  request cannot overwrite newer state.

  One new devDependency, `happy-dom`, and nothing else: seven packages
  against jsdom's tree, declared per-file with a
  `// @vitest-environment happy-dom` docblock so the rest of the suite keeps
  running with no DOM and keeps failing loudly when it reaches for a global
  it did not install. No request-mocking library — `globalThis.fetch` is a
  `vi.fn()` — and no renderer library: React 19 exports `act` itself, so
  mounting a hook on `createRoot` is thirty lines.

  Mutation-tested rather than merely run, on the precedent set by
  `filters.ts`: 45 behavioural mutations, 39 caught. The six survivors are
  written up in the pull request and are not coverage gaps — two are
  equivalent mutants, and four are one half of a pair of staleness guards
  where removing *both* is caught and removing *either* is not, which is
  the hook's defence-in-depth working as documented.
- **A malformed CSRF cookie breaks every write in the app, and reports it
  as a network outage.** `readCookie` ends in `decodeURIComponent`, which
  throws `URIError` on a stray `%`; the throw escapes the request
  middleware before `fetch` is reached, and `guard` in `endpoints.ts`
  normalises anything thrown into `ApiError(0, 'Could not reach the
  server.')`. The user sees an offline message, no request is sent, and
  retrying cannot help. The server never writes such a value — the token is
  base64url — but the cookie is deliberately not `HttpOnly` (that is the
  double-submit mechanism) and carries no `__Host-` prefix, so a sibling
  subdomain can set one, the same exposure already recorded for the OAuth
  state cookie. Recorded rather than fixed, with two `it.fails` tests
  asserting the behaviour we want, so that whoever hardens `readCookie`
  gets an "expected to fail but passed" and deletes the markers.

### Changed

- **`verify-image.sh` now accepts the set of refs this workflow signs from,
  not one member of it.** A keyless certificate's subject ends in the ref
  that produced it, so a release signed on `refs/tags/v1.1.0` fails a check
  pinned to `…/ci.yml@refs/heads/main` — which is what the script did, and
  would have failed the publish job's own verification step on the first
  tagged build. `--certificate-identity` becomes
  `--certificate-identity-regexp` over exactly `refs/heads/main` or a
  `vMAJOR.MINOR.PATCH` tag. It is anchored at both ends because cosign
  applies the pattern with an unanchored `MatchString` — read in
  `pkg/cosign/verify.go` rather than assumed — so without `^` and `$` a
  subject merely *containing* the string would pass, including
  `https://evil.example/https://github.com/Darkflib/…`.
  `tests/test_verify_image_identity.py` pins thirteen rejections against five
  acceptances, and the leading anchor was removed once to watch it go red.
- **The smoke test reads the unit files before it trusts itself.** It ran as
  the three least-privilege roles already, and that was taken to mean a
  reverted cutover would fail CI. It would not have: the harness has no podman
  secrets — under `CONTAINER_ENGINE=docker` it cannot have any — so it invents
  its own connection strings, and nothing in it ever opened a file under
  `deploy/quadlet`. Every assertion would have gone on passing with all four
  units pointed back at the superuser. It now asserts, before starting a
  container, that each unit names the credential the corresponding container
  is about to be handed, that none consumes `sre-tab-database-url`, and that
  `sre-tab-db.container` still bootstraps the superuser. Watched failing under
  four mutations: the whole cutover reverted, the session sweep left behind,
  the backup half-cut, and the migration unit handed the application's role.
- **`restore.sh` proves the restore credential works before it drops
  anything.** It already refused to proceed when the restore *role* did not
  exist, on the reasoning that discovering it after `DROP DATABASE` destroys
  the thing you were about to fail on. Existing is not the same as usable: a
  password rotated by `create-roles.sh --rotate` against a stale
  `sre-tab-migrate-database-url` passes the existence check and then fails at
  `pg_restore`, on the far side of the drop, with an empty database and a
  `password authentication failed` that names the wrong problem. The
  credential is now exercised with a real connection on the path `pg_restore`
  will take, before the confirmation prompt.
- **The cutover runbook's readiness loop can fail.** It polled `/healthz`
  sixty times and then continued regardless, so an application that never
  became ready read exactly like one that did — inside a step headed "do not
  trust the prompt returning".
- **The smoke test waited for the wrong PostgreSQL.** Its readiness loop ran
  `pg_isready` with no host, which talks to the unix socket — and the official
  image's entrypoint bootstraps a cluster by starting a *temporary* server
  with `listen_addresses=''`, reachable on that socket and nowhere else. So
  the loop answered "ready" during the bootstrap, the entrypoint then shut
  that server down to start the real one, and whatever connected next got
  `FATAL: the database system is shutting down`. The race predates this
  release and never fired, because the step after the wait was always a
  container start slow enough to outlast the restart; applying `roles.sql`
  connects immediately, and CI failed on the first run that did. The wait now
  asks over TCP, which only the real server is listening on. Confirmed by
  sampling both probes through a bootstrap rather than by reasoning about it:
  there is a window where the socket says ready and TCP refuses.
- **`install.sh --start` checks all seven secrets, not the pre-cutover four.**
  A guard naming the old set would have passed and then watched three units
- **`create-roles.sh` writes four secrets for three roles.**
  `sretab_readonly` has two consumers that want one password in two shapes —
  `pg_dump` takes `PGPASSWORD`, `sre-tab status` takes a `DATABASE_URL` —
  so `sre-tab-readonly-database-url` is written beside
  `sre-tab-readonly-password` from the same generated password. They are one
  credential and are treated as one throughout: `--rotate` moves both, and a
  role whose secrets are only partly present is refused as drift exactly as a
  role with no secret is, naming the one that is missing. A rotation that
  moved one and not the other would leave the nightly backup and the hourly
  health check on different passwords, with only whichever ran next failing.
- **The smoke test covers the status check the way it covers the sweep.** It
  runs `sre-tab status --failures-over 3` — the unit's own command — as
  `sretab_readonly`, and asserts both halves: the seeded catalogue reads back
  on a role with no write privilege, and a planted `source_status` row four
  failures deep makes the command exit non-zero and name the source, which is
  the half a check that always exited zero would have passed. The unit-file
  step gained the status unit, and `sretab_readonly` is now asserted unable to
  `DELETE` as well as unable to `INSERT`: the sweep and the status check run
  the same image on the same kind of timer and differ only in credential, so
  swapping them fails CI rather than silently stopping the sweep deleting.
  Watched failing first — the status unit pointed back at
  `sre-tab-database-url` and then at `sre-tab-app-database-url`, and
  `sretab_readonly` given `DELETE` in `roles.sql`'s default privileges.
- **`install.sh --start` checks all eight secrets, not the pre-cutover four.**
  A guard naming the old set would have passed and then watched four units
  fail to resolve a `Secret=` reference. When a role secret is the missing
  one it prints the first-install ordering, which is genuinely
  counter-intuitive: `create-roles.sh` installs the roles against the
  *running* database, so a fresh host has to start `sre-tab-db.service` on its
  own, install the roles, and only then run `--start`.
- **`restore.sh` restores with a split credential.** `DROP DATABASE` and
  `CREATE DATABASE` keep the superuser (`--user`/`--password-secret`,
  unchanged defaults), because database-level administration is not
  something any of the three least-privilege roles holds or should;
  `pg_restore` itself now runs as `sretab_migrate`
  (`--restore-user`/`--restore-url-secret`), which needs exactly the rights
  `alembic upgrade` needs. The alternative — granting `sretab_migrate`
  `CREATEDB` so one credential could do both — was rejected on the ground
  that it permanently widens the role the migration unit runs unattended on
  every deploy, cluster-wide, to buy convenience in a break-glass procedure
  a human runs with host root in hand. `roles.sql` is re-applied either side
  of the restore, because grants and default privileges live inside the
  database the restore drops; without that the application comes back to a
  database it cannot read. A host without the roles installed passes
  `--restore-user sretab`, and a missing role is now a message naming both
  ways out, raised on the administrative connection before anything is
  dropped rather than as `password authentication failed` with the database
  already gone.
- `deploy/install.sh` installs `deploy/systemd/*.service` as well as
  `*.timer`. The alert template is a `.service`, so the old glob would have
  installed the timer that fires the check and left every `OnFailure=`
  pointing at a unit that does not exist. The enable loop still globs
  `*.timer` only, deliberately: widening it would not have failed loudly —
  measured on systemd 257, `systemctl enable` on a bare template prints "not
  meant to be enabled" and then exits **zero**, so under `set -e` the loop
  would have gone on reporting a clean install while enabling nothing.
- `deploy/scripts/promote.sh` moves five application Quadlets, was four.
  `UNITS` is an explicit list rather than a glob, so a unit missing from it
  is not a slow drift: CI greps every `Image=` line under `deploy/quadlet`
  and fails unless exactly one distinct reference exists, which means the
  next promotion would break the build having left the new unit on the
  previous digest.
- **The shipped and tested interpreter is Python 3.14.** `.python-version`
  and both Containerfile stages now agree on `python:3.14-slim-trixie`, and
  the workflows read `.python-version` rather than carrying a copy, so
  setup-python and uv cannot select different interpreters again.
  `requires-python` stays `>=3.12` as the floor the code supports. Renovate
  calls a CPython feature release a *minor* update, so the weekly group had
  moved the workflow steps alone; interpreter bumps are approval-gated now,
  and 3.15 arrives as a dashboard tick rather than as a PR.

### Security

- **The deployment no longer connects to PostgreSQL as a superuser.** Every
  Quadlet unit but the database itself now uses one of the three
  least-privilege roles: the application and `sre-tab sessions prune` as
  `sretab_app` (`sre-tab-app-database-url`), the migration unit as
  `sretab_migrate` (`sre-tab-migrate-database-url`), and the backup as
  `sretab_readonly` (`PGUSER` plus `sre-tab-readonly-password`).
  `sre-tab-db.container` keeps `POSTGRES_USER=sretab`, because the superuser
  has to own the cluster and is what `create-roles.sh` installs the other
  three with. This closes the one accepted finding in `ROADMAP.md` whose
  severity three operators did not cap: an application-level SQL injection
  reached `COPY … TO PROGRAM`, which executes commands under the postmaster,
  and now does not.

  The session sweep takes the application's role rather than the migration
  unit's: it is a `DELETE` on one table, and it is the unit that runs
  unattended on a timer with nobody watching.

  It landed as one commit touching nothing but `deploy/quadlet/`, so the
  rollback is one `git revert`, `install.sh`, and a restart — executed, not
  described. `sre-tab-database-url` and `sre-tab-postgres-password` are
  deliberately left in place and are what the rollback returns to; do not
  delete either.

  Demonstrated from inside the running application container on a Debian 13
  host rather than only in a harness: `current_user` is `sretab_app`,
  `is_superuser` is `off`, `CREATE TABLE`, `COPY … TO PROGRAM`, and
  `TRUNCATE` are refused, and the `DELETE` the application needs is not. A
  full cold install proved the rest — a migration creating tables the
  application can use with no manual `GRANT`, a restorable
  `sretab_readonly` dump with its sequences intact, and a session sweep that
  really deletes. `deploy/README.md` carries the ordered rollout runbook for
  an existing deployment and `deploy/ROLES.md` the reasoning.

  The last consumer to move was `sre-tab-status.container`, the hourly source
  health check, which arrived on another branch still naming the superuser's
  `DATABASE_URL` — the two could not edit each other's files, and the unit
  file check in `smoke.sh` is what caught the gap once they met. It now
  connects as `sretab_readonly`, because `sre-tab status` is two `SELECT`s
  and never commits. It needed a fourth secret rather than a fourth role:
  `sre-tab-readonly-password` holds a bare password for `pg_dump`'s
  `PGPASSWORD`, and the CLI wants a whole URL, so `create-roles.sh` now also
  writes `sre-tab-readonly-database-url` from the same generated password.
  Putting the check on `sretab_app` would have been one line and would have
  given an unattended hourly job write access to every table because a
  credential was in the wrong shape.

### Fixed

- **A concurrent first sign-in no longer 500s one of the two callbacks.**
  `upsert_user` was select-then-insert on `github_id`. Two sign-ins for one
  GitHub account racing on that account's *first* sign-in both found no
  row, both inserted, and the unique constraint handed the loser an
  `IntegrityError`. The table was never at risk — the constraint did
  exactly its job — but one of the two users got an error page. It is now a
  single `ON CONFLICT (github_id) DO UPDATE ... RETURNING`, which folds the
  profile refresh into the same statement, so the create path and the
  update path stopped being two branches that have to be kept in step.

  **The distinction worth remembering is DO NOTHING against DO UPDATE, and
  the obvious argument for it turned out to be wrong.** ROADMAP.md proposed
  reusing `insert_ignore` — `ON CONFLICT DO NOTHING` followed by a `SELECT`
  — and the objection raised to that was that DO NOTHING takes no lock on
  the conflicting row, so the loser would affect zero rows, still not see
  the winner's uncommitted row, and fall out holding `None`. Measured
  against PostgreSQL 18, that is not what happens: speculative insertion
  waits on the conflicting transaction, and the follow-up `SELECT` is a
  separate statement with a fresh snapshot, so it reads the committed row.
  The pairing works. What is actually wrong with it is narrower and much
  quieter — it works *because* the connection is at READ COMMITTED, which
  nothing in `create_db_engine` sets and no test asserts, and under
  REPEATABLE READ the same pair raises a serialization failure instead.
  `DO UPDATE ... RETURNING` hands back the surviving row from the statement
  that resolved the conflict, so there is no second snapshot for its
  correctness to rest on, and no unreachable `None` branch whose only
  correct handling would be a retry loop.

  One finding fell out of this that would otherwise have shipped silently.
  `users.updated_at` carries `onupdate=func.now()`, and that is an
  ORM-flush hook: SQLAlchemy does not fold it into a hand-written
  `on_conflict_do_update` set clause. Left out, the column freezes at its
  insert value on every later sign-in and nothing complains. It is set
  explicitly now, and the test guarding it back-dates the row first rather
  than comparing two timestamps taken moments apart — SQLite's
  `CURRENT_TIMESTAMP` has one-second resolution, so the naive version of
  that test passes against the broken code.

  The race test is in `tests/postgres/`, because two connections holding
  write transactions open at once is precisely what SQLite cannot do. Both
  guards were made to fail on purpose before being believed: the race test
  against the restored select-then-insert body (`UniqueViolation` on
  `uq_users_github_id`), and the timestamp test against an update mapping
  with `updated_at` removed.
- **The reason logged for a refused IPv4-mapped literal came from the
  interpreter rather than from the guard.** `classify_address` returned the
  first `ipaddress` predicate that matched, and which of those consult
  `ipv4_mapped` varies by patch level: on Ubuntu 24.04's CPython 3.12.3 —
  which CI reached by accident, through uv's fallback to the runner's system
  Python — `is_loopback` does not, so `::ffff:127.0.0.1` classified as
  "private" and `::ffff:8.8.8.8` as "reserved" rather than "loopback" and
  "blocked-range". The guard now unwraps the embedded IPv4 itself, falling
  back to "blocked-range". Every one of these was refused before and is
  refused now; only the label an operator reads had moved.
- Feed image URLs are validated as strictly as item URLs. The two
  functions now share one host rule, so they cannot drift apart again.
- **An IP-literal check that missed hex-obfuscated addresses**, found while
  fixing the above and wider than it. `_looks_like_ip` tested
  `all(part.isdigit() …)`, and `"0x7f".isdigit()` is `False`, so
  `https://0x7f.0.0.1/…` was not recognised — and that check also guards
  `normalise_item_url`, so a **canonical URL** resolving to the reader's
  own loopback was being accepted and rendered as a link. The check now
  calls `urlguard.parse_numeric_ipv4`, one body rather than a third copy.
- `loadMore` in `usePagedResource` now has a lifecycle. It built an
  `AbortController`, passed the signal, and never aborted it — so nothing
  could cancel a load-more, and an unmount mid-load left the request
  running. Aborted on unmount and on a cache-key change, with the
  `signal.aborted` guard the initial-page effect already had, so a
  cancelled request cannot raise an error banner for something the user
  caused by navigating.
- `loadMore`'s re-entrancy guard read `loadingMore` from React state, which
  only updates on the next render, so two synchronous calls both saw a
  stale `false` and both started a request. It reads a ref now.
- `patchEntry` and `removeEntry` were declared `useCallback(…, [])` with no
  cache-key guard, unlike every other write in the file. Reachable:
  `BookmarksPage`'s optimistic-remove failure path calls `reload()`, which
  bumps the key, so a sibling mutation's revert closure could land on the
  new generation and patch or remove an unrelated row that reused an id.
- `install.sh` staged every `*.timer` but enabled only the backup one by
  name, so a new timer would have been installed and silently never run.
- The image-pin gate's comment in `ci.yml` said the digest was "present in
  all three units"; the session sweep makes four. The comment now describes
  the count-based check it actually performs, rather than a number that
  goes stale each time a unit is added, and points at `promote.sh`'s
  `UNITS` as the list that does need editing.

## [1.0.0] - 2026-08-29

The v1 scope in [prd-v1.md](prd-v1.md), as built and deployed. Tagged at
`700bea3` on `main`. Everything below had accumulated under `[Unreleased]`
since the repository was created; the release changes no code, and exists
because a supply chain that signs, attests, and digest-pins every artefact
was still unable to say which version had shipped.


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
  taking the suite to 187. They pin the distinction between "no override"
  (`null`) and "nothing selected" (`[]`), including its survival through
  the URL, and the thresholds behind the high-volume flag and the
  dominance notice.
- Source and topic slugs are validated when they are added. `sre-tab
  sources add` and `sre-tab topics add` require lower-case letters and
  digits joined by single hyphens, within the column's 64 characters, and
  `sre-tab status` reports any slug that predates the check and exits
  non-zero. A slug goes into the browser's query string, the client's
  cache key, and the feed query, and those consumers disagree about what
  punctuation means — a slug containing a comma produced a source that
  listed correctly and filtered to nothing.
- The API contract is checked against the two committed artefacts the
  client is built from. `tests/test_openapi.py` compares
  `frontend/openapi.json` against the schema the application serves, byte
  for byte, and the `frontend` CI job regenerates `src/api/schema.d.ts`
  and fails on a diff. Regenerating both was previously a manual step
  held together by a sentence in `frontend/README.md`; a contract change
  that skipped it left the client typed against a server that no longer
  existed, with `tsc` still passing because it was checking the client
  against the stale copy.
- `LICENSE` (MIT), matching the declaration that was already in
  `pyproject.toml` but had no corresponding grant in the repository.
- `deploy/README.md`'s procedures are executable rather than prose. Seven
  blocks carry `docs:run` markers — host preparation, configuration,
  secrets, first start, verification, network replacement, and an
  assertion that the recreated address range starts above Caddy's pinned
  `.20` — and run end to end on a Debian 13 host with podman 5.4.2. Two
  commands changed so the document can be run as written: the client
  secret's path is a named variable rather than `/path/to/…`, and the
  non-interactive form of the `app.env` edit is documented alongside
  `sudoedit`. Not run by CI: Ubuntu's `conmon` lacks journald support and
  the long-running units set `LogDriver=journald` deliberately, so a
  GitHub runner cannot start the stack without testing a different
  deployment. See `CONTRIBUTING.md` for the command.
- `CONTRIBUTING.md`, and a `Docs` workflow that extracts the README's
  quickstart from the README itself and executes it on a clean checkout
  on every push. Two documented procedures here have been wrong while
  reading perfectly, so the documentation is executed rather than
  proofread.
- Explicit anchors for every linked-to heading, and a `Docs` check that
  enforces them. A link's fragment must name an `<a id="name"></a>` the
  target document declares at column zero on its own line; the check is an
  exact string match rather than a reimplementation of GitHub's heading-slug
  rule, which is not a documented contract and which the obvious
  implementation gets wrong. Seven anchors added, nothing renamed: GitHub
  still generates its own heading anchors, so every existing link keeps
  working, and a declared id now survives the heading being reworded.
- Both workflows now also run weekly and on demand, not only on a diff.
  The dependency audit, the container build, and the executed quickstart
  all answer questions whose answer changes with no commit behind it — a
  newly published CVE, a base image that moved, an upstream the
  quickstart calls — and a gate wired only to pushes is silent about all
  of them between commits.

### Changed

- **The application image is pinned by digest.** The three application
  units tracked `:latest` with `Pull=newer`, so any restart adopted
  whatever CI had last pushed to main. They now pin
  `:sha-<commit>@sha256:<digest>` with `Pull=missing`; upgrading is a
  reviewed commit produced by `promote.sh`, not a side effect of
  restarting. See the upgrade procedure in `deploy/README.md`.
- **The application image's healthcheck interval is 10s, was 30s.**
  `sre-tab.container` gates on `Notify=healthy` and Caddy is ordered
  after it, so the interval set the deploy window rather than just the
  monitoring cadence: the first check runs one whole interval after
  start, whatever `--start-period` says. `systemctl restart` of the four
  application units returns in 15.4s rather than 35.6s. It does not fix
  the full outage — see `deploy/README.md`, which now carries the
  measurements and the ~20s tail that remains unexplained.
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
- `ALLOWED_GITHUB_IDS` ships **empty** in `deploy/app.env.example`, and
  configuring it is now a documented step of the install rather than
  something to notice. It briefly shipped populated with the upstream
  operators, which worked out of the box for them and for anybody else:
  GitHub user IDs are global rather than scoped to an OAuth application,
  so a self-hoster who registered their own app and replaced every
  credential in the file would still have been authorising three
  accounts they had never heard of.
- Dark and light themes meet WCAG AA on interactive boundaries, not just
  on body text. Button, input, and inactive-chip borders sat at 1.80:1 in
  dark and 1.95:1 in light against 1.4.11's 3:1, and read-card summary
  text at 3.22:1 against 1.4.3's 4.5:1 — the kind of failure a screenshot
  does not show, because the text on top of them was always legible. A
  `--focus-halo` token also separates the focus ring from the fill it
  sits on: in dark, `--focus` and `--accent` were the same colour, so a
  focused active chip was a glow rather than a ring.

### Security

- **Exceptions are constructed per raise, never shared.**
  `get_current_user` and the sign-in rate limiter each raised one
  module-global `HTTPException`. Python appends a frame to
  `__traceback__` on every raise and a module global is never collected,
  so each 401 permanently pinned its `Request`, its raw token, and its
  `Session`: 2,000 unauthenticated requests grew the object to 18,009
  frames and the process by 65.4 MB — 32,719 bytes per request, on
  `/api/v1/me`, which needs no credentials and has no rate limiter.
  Against `MemoryMax=768M` that is roughly 23,000 requests to a cgroup
  kill. The 429 path was worse in kind if not in size: it is raised at
  the top of `github_callback`, where `code` and `state` are bound, so
  live OAuth codes were retained in frame locals.
- **Feed parsing is bounded by element count, not just by body size.**
  `MAX_ENTRIES` capped what was kept and never the parse that produced
  it, and the document was expanded twice — once by defusedxml as a
  gate, once by feedparser. A valid 5.24 MB feed inside
  `source_fetch_max_bytes` cost about 97 MB and 2.3 seconds to reduce to
  500 entries, in front of a serial refresh loop. The gate now streams,
  which keeps the same guarantee — the `forbid_*` checks fire on parser
  events rather than on a finished tree — and refuses a document over
  `MAX_ELEMENTS` or `MAX_ENTRY_ELEMENTS`: 0.01 seconds and 1 MB for the
  same feed. Entry count alone would not have been enough, since a
  document of tiny non-entry elements has no entries and costs the same.
  Attributes are capped per element separately, because that one bounds
  a stall rather than an allocation: feedparser is quadratic in the
  attribute count of a single tag, so 0.65 MB carrying 60,000 attributes
  on one element — inside every other limit, and an eighth of a permitted
  body — stopped a refresh cycle for 21 seconds. The gate reaches the
  same document in 0.03s, so the cost was only ever downstream of it.
- **DNS resolution is inside the fetch deadline.** `getaddrinfo` takes no
  timeout and ignores `socket.setdefaulttimeout`, and was called before
  anything consulted the clock — bounded by `resolv.conf` rather than
  unbounded, but ten to forty seconds in front of a serial refresh loop
  is one slow resolver stalling every source behind it.
- **`Strict-Transport-Security` is set, mirrored, and verified.** It was
  absent from `app/middleware.py`, from the Caddyfile mirror, and from
  the list of headers `deploy/README.md` tells the outer proxy not to
  strip. `max-age=31536000` without `includeSubDomains`, which is
  correct for the documented single-host topology and a year-long
  outage for an apex deployment; the README says which is which.
- **The setuid strip fails closed.** The layer that removes every setuid
  and setgid bit — the stand-in for the `NoNewPrivileges=true` that
  `sre-tab.container` cannot set — ended in `|| true`, and nothing
  downstream checked. It now asserts the resulting filesystem, runs
  after every `COPY` rather than before them, and CI asserts the same
  property against the built image.
- **The signed image is the tested image.** `publish` rebuilt from the
  Containerfile on its own runner, so the signature, the SLSA
  provenance, and the SBOM all described bytes no smoke test had seen.
  The tested image now travels between the jobs and `publish` refuses to
  push anything whose image ID is not the one `container` tested.
- **`restore.sh` stops the backup timer.** It stopped the application but
  not the schedule, and between `DROP DATABASE` and `pg_restore`
  finishing the database is empty and perfectly healthy — so a backup
  landing there dumps nothing, passes `backup.sh`'s own validation, and
  is promoted to a final dump with a checksum and today's date. The timer
  comes back only when the restore actually finished: an interrupted or
  failed one leaves it stopped, and says so, rather than handing the next
  backup a database in an unknown state.
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

- **"Save as my default" no longer inverts an empty selection.** It wrote
  the resolved chip state into preferences, so deselecting every source
  and saving stored an empty saved selection — which the server reads as
  "no preference, use the instance defaults". The user's "show me
  nothing" became "show me everything" in two clicks. An empty selection
  is a step towards a filter rather than a filter, so the control is now
  unavailable while nothing is selected and the filter bar says why.
- **"Save as my default" no longer pins a snapshot of today's
  catalogue.** It wrote both dimensions from the resolved chip state, so
  a dimension the user had not overridden was saved as an explicit list
  of everything currently in the catalogue — after which a source added
  later never appeared for that user. Only the dimensions the user
  actually changed are sent.
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
