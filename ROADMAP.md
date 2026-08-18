# Roadmap

Work deliberately deferred past v1. Items are grouped by why they were
deferred, not by size. See [prd-v1.md](prd-v1.md) for v1 scope and
[PLAN-v1.md](PLAN-v1.md) for how it was built.

Items marked **landed** stay listed rather than being deleted: the reasoning
that put them here is usually still worth reading, and a roadmap that only
ever grows tells you nothing about what moved.

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

## Operations

- **Off-host backups.** `/srv/sre-tab/backups` sits on the same host as the
  database. That is a backup, not disaster recovery.
- **`OnFailure=` alert unit.** Deliberately not invented in v1 — orbit-data's
  equivalent is a subcommand of its own application, and sre-tab had no CLI
  at the time. It has one now, and `sre-tab status` already exits non-zero
  when an enabled source is failing, so the alert path has something to call.
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

## Documentation

- **The README's quickstart is executed on every push** — **landed.**
  `.github/workflows/docs.yml` extracts the commands from `README.md` itself
  rather than copying them, so the workflow cannot pass while the document it
  protects has stopped being true. Two wrong procedures preceded it:
  `install.sh --start` never recreated a removed network, and the documented
  upgrade sequence was wrong as written.
- **`deploy/README.md` is not executed.** Its procedures need a Podman host,
  root, and live systemd. `smoke.sh` covers the migration, health, backup, and
  restore paths through the same scripts an operator runs, but the install,
  secret, upgrade, and network-replacement sequences are prose verified by
  hand. Extending the same marker-and-extract approach to a Linux runner is
  the obvious next step, and the expensive part is a runner with systemd
  rather than the harness.
- **Make `Docs` a required check** — **landed, as a side effect of fixing the
  branch-protection rule.** Both of its check-runs — `README quickstart runs
  on a clean checkout` and `Relative links resolve` — are in the required set
  of eight, listed in
  [CONTRIBUTING.md](CONTRIBUTING.md#branch-protection). The rewrite that
  corrected the job-key/display-name mistake replaced the context list
  wholesale, so this was picked up in the same write rather than as its own
  task.

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

- **Issue and pull-request templates.** Neither exists. Worth adding if the
  repository attracts contributions beyond the operator's own.

## Product

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
