# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Muted words and topics, so a feed can be told what not to show.**
  `PATCH /api/v1/me/preferences` gains `muted_words` and `muted_tags`, both
  replace-the-whole-list like `topics` and `sources`, and Settings gains
  the list that manages them. Anything muted leaves the feed everywhere.

  **It is the search predicate negated**, which is why search landed
  first: one `_text_match` serves both, so the two cannot drift apart
  about stemming, case folding, or what a word boundary is. Words match
  the item's title and summary; a muted phrase needs all of its words, so
  "premier league" hides the league and not every mention of a premier.
  Topics match the item's topic slugs.

  **Bookmarks are never muted.** A bookmark is an explicit "keep this" —
  the argument that already exempts bookmarked items from retention.

  **A muted topic is validated against the catalogue and a muted word is
  not**, and the asymmetry is deliberate: muting language the catalogue
  has never heard of is what word-muting is for, while a muted topic
  matching nothing is a typo that would report success and mute nothing.

  Terms are case-folded, whitespace-collapsed and deduplicated, and a term
  that normalises to nothing is dropped rather than stored — an empty term
  is a substring of every item, so storing one would empty the feed with
  nothing on screen to say why.

  **Muting is the only narrowing with no evidence of itself on the feed**,
  so the filter bar now says how much is muted whenever anything is. When
  every word of the current search is also a muted word it says something
  stronger, because that page is provably empty before the request is
  made — the reader is told rather than left to wonder.

  Four defects found in review before this shipped, all of them the same
  kind — a guard, or a claim, that was true of something adjacent to what
  it described. A term can grow past its column while being normalised,
  because `casefold` is not length-preserving and sixty-four `ß` become a
  hundred and twenty-eight `s`; the length is now re-checked after
  normalising rather than only before, which on PostgreSQL is the
  difference between a 422 and a 500. The "nothing can match" notice
  analysed every word of a query when the server searches only the first
  eight. The first committed search replaced the history entry it started
  from, so Back left the feed instead of returning to it unsearched. And a
  topic an operator disabled kept hiding items for anyone who had muted
  it, with no control left anywhere to turn it off — muted topics are now
  listed and removable whether the catalogue still carries them or not.

  Two more from a second reviewer. A whitespace-only term normalised to
  nothing and was dropped, which turned `["  "]` into the wire form of
  "unmute everything" — so a request that looked like adding one mute
  removed every mute the reader had; such terms are refused now. And the
  topic list had no equivalent of the hundred-term limit the word list
  carries, so checking one more topic at the limit produced a failed save
  and a checkbox that sprang back.

- **Search over the retained items — `GET /api/v1/feed?q=`, and a box in the
  filter bar.** `feed_retention_days` defaults to 90, so there has always
  been a real corpus behind the feed and no way to reach anything in it but
  to scroll.

  **It is one more predicate in the query that already runs, not a second
  endpoint.** `search_predicate` joins the read-state and source filters
  inside `_apply_filters`, so the `LIMIT` is taken after the narrowing:
  pages stay full, a cursor still names a row that satisfies the filter, and
  a search composes with every other control rather than replacing them.
  That is also what makes it the right thing to build first — muting words,
  which is next, is this expression negated.

  **Two engines, and the difference is documented rather than smoothed
  over.** PostgreSQL matches `plainto_tsquery` against a `to_tsvector` of
  title and summary, backed by a GIN index (revision `b7c1e0a94f6d`);
  SQLite, which is development only, requires each term as a case-folded
  substring. So PostgreSQL stems — `bookmarks` finds "bookmark", and `cat`
  does not find "catalogue" — and SQLite does not. `plainto_tsquery` rather
  than `websearch_to_tsquery` because that one's quoted phrases and `-`
  exclusions have no honest counterpart in the SQLite branch, and a
  divergence in *semantics* between the engine a developer runs and the one
  that serves anybody is worse than one in recall.

  **Results stay in publication order.** The cursor *is* `(published_at,
  id)`, so relevance ranking needs a different key and would invalidate
  every cursor already issued.

  **The index is verified by its query plan, not by its results.**
  PostgreSQL uses an expression index only when the indexed expression
  matches the query's character for character, and a divergence is silent
  in every direction that can be observed cheaply: the index builds,
  `CREATE INDEX` reports success, results are correct, and every query is a
  sequential scan. `tests/postgres/test_search.py` therefore asserts the
  plan with `enable_seqscan` off, so the question is whether the index *can*
  serve the query rather than whether the planner prefers it on a small
  table.

- **The feed uses the width the screen has.** `.shell__main` and
  `.shell__bar` were capped at 1180px, which against the card grid is three
  columns and then an empty margin on anything wider than a laptop. Both now
  take `--shell-max`, at 106rem — five columns plus the shell's own padding,
  set in `rem` so it tracks the root font size the cards are already sized
  against. Measured at 1920×1080: three columns to five, and 61% of the
  viewport to 88%. `.settings` and `.bookmarks` keep their 60rem reading
  measure and are now centred rather than left-aligned.

- A social preview image, which closes the last of the discoverability items
  and the whole Repository section of `ROADMAP.md`. Its source is committed
  at `docs/images/github-social-preview.png`, and the reason that is worth
  saying is that committing the file is *not* what sets the preview —
  uploading it under Settings → General is. GitHub exposes no API for it, so
  the two can drift and nothing in this repository can detect it. Presence
  was therefore established rather than assumed: `og:image` on the
  repository page resolves to `repository-images.githubusercontent.com`,
  where an uploaded preview is served, rather than the
  `opengraph.githubassets.com` default.

- **Per-user API tokens, so the API can be called from somewhere other than
  the browser.** Everything under `/api/v1` was reachable only with the
  session cookie, which meant a script, a status board, or a terminal had no
  way in that was not "hold a GitHub session". A token is created from
  Settings, sent as `Authorization: Bearer …`, and belongs to a user rather
  than to the instance — there is no operator half and no CLI in this change,
  because a token is not an operator's concern.

  **Two scopes, chosen at creation and not defaulted.** Read-only and full.
  The split is the whole blast radius of a leak: a read-only token in a log
  file discloses what someone reads, a full one is their account. The schema
  models it the way `Theme` and `Layout` are modelled — `Enum(…,
  native_enum=False)` — so the database refuses a value the application does
  not know, and a request that omits the field is a 422 rather than the
  convenient scope.

  **The scope column carries a CHECK constraint**, which `theme` and
  `layout` do not. `Enum(native_enum=False)` renders as `VARCHAR` and emits
  no constraint of its own — `create_constraint` has defaulted to `False`
  since SQLAlchemy 1.4 — so without it a restore or a hand-written `UPDATE`
  could store a scope the application does not know, and the first request
  presenting that token would fail materialising the row with `LookupError`:
  a 500 out of the authentication path, from a column believed to be
  constrained. Asserted against the migrated schema, the `create_all` schema,
  and PostgreSQL, each with a valid insert first so a table that refused
  every write could not pass.

  **The value is shown once and stored as a SHA-256 digest**, the same
  discipline `sessions.token_hash` already uses, so the server cannot produce
  it again and a database leak yields nothing presentable. The row keeps a
  short non-secret `display_prefix` so two tokens can be told apart in the
  UI, an optional `expires_at`, a `revoked_at`, and a `last_used_at` — the
  last of those because a forgotten token should be *visible* rather than
  merely present. Every token starts `sretab_pat_`, which is a fixed prefix
  rather than entropy: it makes one greppable and gives secret scanners a
  literal to match. The random part is 256 bits from `secrets`.

  **The allow-list is now re-checked on every token request.**
  `allowlist.is_authorised` used to run once, at the OAuth callback, because
  that was the only moment authorisation was decided. A token outlives that
  moment by design, so an account removed from `ALLOWED_GITHUB_IDS` would
  have kept a working credential — a way back in for the one person an
  operator has just decided to remove. Removing the id now kills the token on
  the next request, with the row untouched, and it fails closed the way that
  module already requires.

  **Scope is enforced in middleware, not in a dependency.** The rule is that
  a read-only token may not use a mutating method, and it has to hold on
  every route including ones written later by someone who has never read the
  file. That is the same shape of rule CSRF is, and it gets the same
  treatment for the reason `csrf_middleware.py` already gives: a dependency
  protects whichever routes remember to ask. No route is consulted, so no
  route can forget. The test that asserts the refusal builds its list of
  mutating operations from the published schema, so a new one is covered
  without anybody remembering to add it — and it was watched failing with a
  route added that the list did not name.

  **Token management refuses a token.** `/api/v1/me/tokens` requires the
  browser session and answers 403 to a bearer credential however privileged.
  A full-scope token can already do everything the API offers, so letting it
  mint and revoke tokens costs nothing an attacker did not have — until the
  leak is noticed, at which point revoking the leaked token accomplishes
  nothing because its holder could have issued a replacement at any time.
  This is the decision in this change most worth arguing with: it is not
  forced by anything, and it is easily reverted by deleting one router
  dependency.

  **A refused token is always the same 401 an absent cookie gets.** Unknown,
  malformed, revoked, expired, and no-longer-allow-listed all fall through to
  one refusal rather than to five matching `raise` statements, on
  `app/api/deps.py`'s existing reasoning. Revoking a token that is not yours
  is a 204 no-op rather than a 404, on the reasoning
  `tests/api/test_isolation.py` pins for bookmarks: a different answer would
  confirm a guessed id.

  **No new rate limiter, recorded as a decision rather than an omission.**
  `app/auth/ratelimit.py` throttles OAuth initiation and callback failures.
  Failed bearer authentication is not throttled: the credential is 256 bits,
  so guessing is not the threat, and the cost of a refusal is one indexed
  lookup on `token_hash` — identical to the session-cookie path, which has no
  limiter either. Adding one only here would throttle a legitimate
  integration sharing an egress address with a failing one, which is exactly
  the case this feature exists for, while leaving the equivalent session path
  open. [ROADMAP.md](ROADMAP.md#scaling) carries the version worth building,
  which covers both credential paths at once.

  One model class, one enum, and exactly one Alembic revision — a deliberate
  crossing of two rules [AGENTS.md](AGENTS.md) marks as considered acts
  rather than forbidden ones. The revision is verified up and down, empty and
  populated, on SQLite and PostgreSQL, one revision at a time in each
  direction so a defect in either downgrade cannot be masked by the other.

- **The published host port is an operator setting, because on the reference
  host it was already a local fork of a tracked file.** `sre-tab-web.container`
  hardcoded `PublishPort=127.0.0.1:8080:8080`, 8080 was taken on the host it
  was deployed to, and the only way to say so was to edit that file in the
  checkout — which `git checkout`, `git stash`, and `git reset --hard` each
  discard without a word, and which makes the next upstream change to that
  file a merge conflict. Leaving the edit in `/etc` was not available either:
  `install.sh` copies every tracked Quadlet over `/etc/containers/systemd` on
  every run, so the fork had nowhere to live but the checkout. Which port a
  service appears on is host policy, and there was no way to express it.

  `SRE_TAB_WEB_PORT` in `/etc/sre-tab/install.env` is that way. The file's
  absence is the default, so a host that never creates one is byte for byte
  the host it was before — and commenting the line out and re-running returns
  a host that had one, because the installer removes what it wrote rather than
  leaving it behind. Only the **host** side moves: the container still listens
  on 8080, where the Caddyfile's site block, the image's healthcheck, and the
  right-hand number in `PublishPort=` have to agree with each other and where
  a host has no stake in the value they agree on.

  **It is a Quadlet drop-in, not a rewrite of the unit.** Quadlet merges
  `<unit>.container.d/*.conf` exactly as systemd merges a `.service.d`
  drop-in — the mechanism this installer already uses one layer up to give
  `sre-tab-backup.service` its `OnSuccess=` — so `sre-tab-web.container` is
  still installed byte for byte from the repository and `diff` between
  `deploy/quadlet` and `/etc/containers/systemd` stays empty on a host that
  has moved its port. The load-bearing line in that drop-in is the *empty*
  `PublishPort=`: Quadlet honours systemd's list semantics, so without it the
  generated `ExecStart` carries both ports and the old one stays open — the
  half of "move the port" that a naive implementation misses, because the new
  port works.

  Three other mechanisms were measured and rejected, and the third is the one
  worth recording. Quadlet does **not** expand variables: it copies
  `PublishPort=127.0.0.1:${SRE_TAB_WEB_PORT}:8080` through to `--publish
  127.0.0.1:${SRE_TAB_WEB_PORT}:8080` verbatim and `podman-system-generator
  --dryrun` reports nothing wrong. systemd *would* then expand it from an
  `EnvironmentFile=` under `[Service]`, mid-word substitution included — that
  was measured working, on a throwaway unit, with the file absent and with it
  naming a port — and it was rejected anyway, on the third case: an empty
  assignment expands to `--publish 127.0.0.1::8080`, which podman accepts by
  publishing on a random ephemeral port. Measured, `8080/tcp ->
  127.0.0.1:37341`, with `systemctl start` exiting 0 and `systemctl --failed`
  empty — the unit up, nothing complaining, and the front door somewhere
  nobody is looking. It would also put the value beyond validation, since it
  is read at container start rather than at install time. A `.service.d`
  drop-in was the third: `PublishPort=` lands inside the generated
  `ExecStart=`, so overriding it there means restating podman's whole command
  line and re-deriving it after every podman upgrade.

  The value is validated before anything is installed, so a bad one leaves the
  host untouched rather than half-written: empty, `0`, `70000`, `8080x`, and
  `08080` are each refused with the value quoted back and exit 2. A port below
  1024 is accepted with a warning rather than refused — rootful podman can
  bind one and the unit publishes on loopback, so there is no measurement here
  that would justify a refusal — and the warning says what it is actually
  worried about, which is that below 1024 is where the host's own listeners
  live, the TLS proxy that forwards to this port included.

- **`ARCHITECTURE.md`: the system in nine Mermaid diagrams.** The README
  carried an eight-line ASCII sketch of the topology and nothing else, so
  every structural question — what a request passes through, what order the
  SSRF guard checks things in, which unit connects to the database as which
  role, how a commit becomes a running container — was answerable only by
  reading the module docstrings that hold the reasoning. Those docstrings are
  good and they are also scattered across a dozen files, which makes them a
  reference rather than an orientation.

  The document draws the deployment topology, the middleware and dependency
  chain, the OAuth exchange, the schema, the refresh tick, the guard's eight
  checks in order, the failure back-off, session ownership, and the path from
  commit to host. It ends with a table naming, for each property this service
  claims, the single place that makes it true — because a property enforced in
  two places is enforced in neither, and one enforced only in prose is not
  enforced at all. Two properties are listed as deliberately absent from that
  table: the backup timer's overnight catch-up and the fetcher's
  accept-a-redirect branch, neither of which anything verifies.

  The topology diagram names 8080 as the default rather than the policy,
  matching the `SRE_TAB_WEB_PORT` setting that landed alongside it — a diagram
  is a claim about the deployment and goes stale the same way prose does, and
  git could not see that one because the two changes touch different files.

  Every diagram was parsed against a pinned Mermaid 11.17.2 and rendered to
  check it is legible rather than merely valid, which caught four that were
  not: a topology whose layout drew an edge that did not exist
  between two external services, a state diagram whose labels overlapped into
  illegibility, and two others too tall to read.

- **A `Docs` job that parses every Mermaid block, and a `CONTRIBUTING.md`
  section saying what it does not cover.** An unparseable diagram is not a
  wrong sentence, it is a red "Unable to render rich display" box where the
  picture should be, on the page a newcomer is most likely to open first, and
  nothing here would have noticed — a reviewer least of all, since the diff
  shows source and rejected Mermaid looks exactly like accepted Mermaid.

  Two properties are worth naming because both come straight from the rule
  about what a green check is worth. **Finding zero diagrams is a failure**,
  since a repository with none and an extractor that has broken print the same
  success line. And the script **self-tests before it reports** — a known-good
  diagram must parse, a known-bad one must not, or it exits saying nothing
  about the corpus. That is not decorative: Mermaid loads DOMPurify at parse
  time and needs a `window`, and without one it does not fail cleanly, it
  fails *partially*, with seven of the nine erroring and two parsing anyway.

  The parser has its own manifest and committed lockfile under
  `.github/scripts`, and the job runs `npm ci`, so the 122 packages this gate
  imports and executes are the same on every run rather than whatever resolved
  that morning. That is a change of position from the first version of this
  work, which installed them ephemerally on the `uvx semgrep==` precedent;
  review pushed back, and the argument that settled it was not the security
  one — it is that a lockfile lets Renovate manage these through its ordinary
  npm manager, which deleted a custom regex manager whose failure mode was to
  silently stop matching. The DOM is `happy-dom`, which the frontend already
  vets, so no second DOM implementation enters the repository; the two
  manifests pin it independently and are not required to agree.

  **`Mermaid diagrams parse` is now the ninth required check on `main`.**
  Branch protection is a GitHub setting rather than a file, so it could not
  land in the same diff; it was written afterwards and verified by
  set-differencing the required contexts against the check-runs the repository
  actually reports, which came back empty.

- **The filter bar collapses, and says what it is still filtering while it
  is collapsed.** Filtering is the primary control on the feed screen and the
  chips are in the page flow deliberately — that argument decided where the
  control lives, and was never an argument about how much of the viewport it
  keeps afterwards. On a phone the chip bar is most of the first screen
  before a single article is visible, and on any screen it is noise once the
  selection is settled. The bar is now a disclosure: a real `<button>`
  carrying `aria-expanded` and `aria-controls`, over a region toggled with
  `hidden` so the chips leave the tab order and the accessibility tree
  together rather than only leaving the paint.

  **The failure this had to avoid is a collapsed bar that filters silently.**
  A reader with three sources selected and no chips on screen reads a short
  feed as a broken one. So the head never collapses — the "Filtered" badge,
  the counts, and "Clear filters" stay put — and a summary line underneath
  names what the counts cannot: "3 of 8 sources" does not say *which* three,
  and the chips that would are exactly what is hidden.

  That summary keeps the three states of a filter dimension apart, which is
  the distinction the whole filter model turns on. `null` means "no override,
  use my saved selection", so it contributes no phrase at all and an
  unfiltered bar summarises to nothing — a phrase there would report a filter
  that does not exist. `[]` means the user deselected everything, which is
  the loudest case rather than the quietest, so it is named outright: "No
  sources selected". A list is named up to three and counted past that. A
  selection that happens to contain the whole catalogue is still an override
  and still says so, because it is pinned and a source added tomorrow will
  not appear in it.

  **The collapsed state is per device, in `localStorage`, and deliberately
  not in the database.** The same account on a phone and on a desktop wants
  different answers and `user_preferences` has no row that can hold two;
  `ROADMAP.md` puts per-device preferences at v2. So no column, no migration,
  and no API change. Expanded is the default and the *absence* of a stored
  value is what says so, which means storage that throws — Safari in private
  mode, blocked cookies — lands on the same answer as storage that is empty:
  the bar a reader had before this existed, not a hidden one nobody asked
  for. The `try`/`catch` discipline `theme.ts` already had is now a shared
  `lib/storage.ts` that both callers go through, rather than a second copy of
  it.

  Nothing animates, so there is nothing here for `prefers-reduced-motion` to
  reduce; the chevron's direction comes from the button's own
  `aria-expanded` rather than from a class the component would have to keep
  in step with it. No keyboard shortcut was added — see the pull request for
  why the shared shortcut table is the wrong place for a control only one of
  the two pages that render it has.

### Fixed

- **`restore.sh` polled a hardcoded 8080 for its post-restore health check.**
  On a host that had moved the published port, a restore that had succeeded
  in every respect ended by declaring the application unhealthy and exiting 1
  — the failure looking exactly like the one thing a restore must never do
  quietly. It now asks `podman port sre-tab-web 8080/tcp` which port the front
  door is on, rather than reading `/etc/sre-tab/install.env`: that file is the
  *next* install's intention, and this loop has to poll the port that is
  published now.

  **An unanswerable question now fails the check rather than falling back to
  8080**, and the first version of this fix got that wrong in a way worth
  recording. It warned and used 8080 anyway, on the reasoning that the
  fallback could only fail closed. It cannot: the hosts that set
  `SRE_TAB_WEB_PORT` are exactly the hosts where something else already owns
  8080 — that is the only reason to move it — so on the one host where this
  question matters, the fallback aims the health check at a *different
  service*. Staged on the test host in that exact shape, with the front door
  stopped and an unrelated 200-answering listener on 8080, the fallback
  version printed `Waiting for the health check on 127.0.0.1:8080... Healthy.
  Restore complete.` and exited 0 while nothing served the dashboard at all.
  The refusal names what it could not determine and exits 1, saying plainly
  that the database itself is restored and verified; the happy path on a moved
  port still passes. Raised in review by CodeRabbit on #27.

- **`check-doc-links.py` treated a fence with an info string as a closing
  fence, and that was a false pass in a required check.** CommonMark is
  explicit that a closing fence carries none. Without the rule, a line like
  ```` ```markdown ```` *inside* a ```` ``` ````-opened block reads as a
  close, and every fence after it pairs one out of step — so prose is read as
  code and a broken link in it is never reported. Probed on a document holding
  a link to a file that does not exist, which the script passed green.

  Found while verifying the same omission in the new Mermaid checker, which a
  review of this pull request flagged. Both are fixed, and
  `tests/test_doc_links.py` now covers the case — it was made to fail against
  the unfixed script first, which is the only reason to believe it.

### Changed

- **`actions/attest-sbom` is deprecated; the SBOM attestation now uses
  `actions/attest`.** The old action still works — it is currently a wrapper
  around the new one, which is why the inputs are unchanged — but a
  deprecated action in the one workflow that publishes signed artefacts is a
  thing that stops working on somebody else's schedule. Renovate would have
  kept bumping its version and never told us it was going away.
  `actions/attest-build-provenance` is *not* deprecated and is untouched.

  The migration is checked by the publish job rather than rehearsed, since a
  publish-only step cannot be rehearsed: `verify-image.sh
  --require-attestations` runs later in the same job and asserts an SBOM
  attestation against the image just pushed. Review pushed back on how much
  that claim was worth, correctly, and measuring it against the published
  1.1.0 image narrowed it twice. `gh attestation verify` matches by digest and
  will accept any attestation the API holds for one, not specifically this
  run's — what makes the check meaningful is that every build produces a new
  digest, so the only attestations that exist for it are this run's. And the
  predicate assertion is a prefix match rather than equality: the recorded
  type is `https://spdx.dev/Document/v2.3` and the verifier asks for
  `https://spdx.dev/Document`. That is deliberate, because pinning the SPDX
  version would turn an SBOM-format bump into an apparent supply-chain
  failure, but it means the check answers "an SPDX document is attested"
  rather than "exactly this version is". A bogus predicate type is still
  refused, which is what keeps it an assertion.

- **`deploy/README.md`'s verification steps ask the front door which port it
  is on** instead of hardcoding 8080, in first start, in the roles cutover,
  and in Operations. A runbook that names the number sends an operator who has
  moved it to a port nothing serves, where a healthy deployment and a failed
  one produce the same output — the exact shape of quiet wrongness the
  executed-documentation gate exists to remove. `${port:?…}` rather than a
  fallback to 8080, so a question that cannot be answered fails saying so
  instead of being answered with a guess.

  The document gained a `docs:run` block that proves the mechanism without
  moving the running host's port: it stages an install into a temporary
  `DESTDIR`, asserts the whole set of published ports is exactly the new one,
  and asserts that removing the setting removes the drop-in.

  **Its first draft was a guard that could not fail, and that is worth
  recording rather than quietly fixing.** It ended `! grep -q -- '--publish
  127.0.0.1:8080:8080' "$stage/dryrun"`, which reads as "assert the old port
  is gone". Deleting the reset line from the installer on purpose produced an
  `ExecStart` carrying both ports — the exact breakage the assertion existed
  to catch — and the block still exited 0. POSIX says `set -e` is ignored for
  a pipeline beginning with the `!` reserved word, so a `!`-prefixed assertion
  is a comment with a trace line. It is now one equality against the whole set
  of published ports, which says the same two things in a form the shell acts
  on, and it was seen to go red on that same sabotage and on a second one that
  stops the installer removing a stale drop-in.
## [1.1.0] - 2026-09-02

### Added

- **Issue and pull-request templates, and a repository that can be found.**
  Neither template existed, and the repository carried no topics, no homepage,
  and no social preview — so it was discoverable by name, by someone who
  already knew the name. The templates ask for the two things that are
  actually hard to get out of a self-hosted bug report: which of the two
  deployment shapes is running, since the quickstart and the Quadlets differ
  by design in ways that look like bugs, and *where the reporter's
  expectation came from*, because a wrong document is a bug this project
  treats as one. Blank issues are disabled and the security advisory channel
  is the first link a reporter sees, rather than something found after
  posting publicly.

  The metadata half is done too: sixteen topics and a homepage pointing at
  the live instance, whose landing page explains the product and says plainly
  that sign-in is operator-restricted, so a visitor who cannot sign in still
  learns what this is. A social preview image is the one piece still missing,
  and it is missing for a structural reason rather than an oversight —
  GitHub exposes no API for it, so it cannot be set from a script or checked
  by anything in this repository, and nothing will ever report that it is
  absent.
- **`AGENTS.md`'s "a green check is not a passed check" rule now covers the
  harder half.** The six instances it named were guards that *could not*
  fail. This release found eleven that could, went red on demand, and still
  answered a question next to the one they were relied on for — `smoke.sh`
  running as the least-privilege roles while never opening a unit file, so a
  reverted cutover would have passed; `restore.sh` proving the restore role
  existed but never that its credential authenticated; `pg_isready` answering
  for the temporary server the postgres image starts during bootstrap. The
  rule now asks the two questions that catch those: what the guard says when
  its subject is missing or empty, and whether it would still pass with the
  thing it protects reverted.

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
  state cookie. Recorded rather than fixed at the time, with two `it.fails`
  tests asserting the behaviour we want — and closed later in this same
  release, which is what the markers were for. See the fix under Fixed
  below for the choice they forced: the raw value, not an absent one.
- **Host prerequisites, because a thin Debian install has no container
  DNS.** `aardvark-dns` is a *Recommends* of Debian's `podman` package, so
  a host built with `--no-install-recommends` has podman, has netavark, and
  cannot resolve one container from another — which is how every hop in
  this stack works: five units reach the database as `sre-tab-db`, and
  Caddy's upstream is `sre-tab-app:8000`. Podman does not fail, it warns
  once (`aardvark-dns binary not found, container dns will not be
  enabled`) and carries on, so the first symptom is a psycopg "could not
  translate host name" traceback out of `sre-tab-migrate.service`, which
  reads like a database that is down. Observed on a fresh Debian 13 host
  with podman 5.4.2, where `deploy/scripts/smoke.sh` failed the same way at
  its first cross-container step.

  `deploy/README.md`'s host preparation now names both packages and
  installs them under a `docs:run` marker, so the throwaway host that
  executes that document bootstraps itself rather than being assumed. And
  `install.sh --start` refuses to start the stack when podman reports no
  aardvark-dns, before the timers are enabled — the check asks
  `podman info` for the path it resolved rather than looking on `PATH`,
  where the binary never is, since Debian puts it under `/usr/lib/podman`
  and other distributions under `/usr/libexec/podman`. A podman that
  cannot answer that query at all warns rather than refuses: a guard
  deciding on evidence it does not have is the wrong failure, and one that
  says nothing is the green check that checks nothing. All three branches
  were exercised against a stubbed `podman` before being believed.

- **Host prerequisites, because a thin Debian install has no container
  DNS.** `aardvark-dns` is a *Recommends* of Debian's `podman` package, so
  a host built with `--no-install-recommends` has podman, has netavark, and
  cannot resolve one container from another — which is how every hop in
  this stack works: five units reach the database as `sre-tab-db`, and
  Caddy's upstream is `sre-tab-app:8000`. Podman does not fail, it warns
  once (`aardvark-dns binary not found, container dns will not be
  enabled`) and carries on, so the first symptom is a psycopg "could not
  translate host name" traceback out of `sre-tab-migrate.service`, which
  reads like a database that is down. Observed on a fresh Debian 13 host
  with podman 5.4.2, where `deploy/scripts/smoke.sh` failed the same way at
  its first cross-container step.

  `deploy/README.md`'s host preparation now names both packages and
  installs them under a `docs:run` marker, so the throwaway host that
  executes that document bootstraps itself rather than being assumed.
  `install.sh --start` refuses to start the stack when podman reports no
  aardvark-dns, before the timers are enabled, and `smoke.sh` refuses
  before it creates its network, beside the Quadlet-credential check that
  already runs there for the same reason — a two-second question whose
  answer, unasked, arrives three minutes in as a connection traceback.
  Under `CONTAINER_ENGINE=docker` the harness says the engine resolves
  names itself rather than skipping quietly.

  Both checks read `podman info --format json` for the string, rather than
  a `--format` template naming the field that holds the path, and that is
  the second version. The template has a third state — one naming a field
  this podman does not carry exits non-zero having reported nothing, which
  is not distinguishable from the binary being absent — and every way of
  handling it is wrong: refusing decides on evidence it does not have, and
  warning through is the green check that checks nothing. Removing the
  state was the fix. The whole document cannot fail that way, and no podman
  at all is the empty document, which refuses like any other host that
  cannot resolve a container name. Neither looks for the binary on `PATH`,
  where it never is: Debian installs it under `/usr/lib/podman`, other
  distributions under `/usr/libexec/podman`, and `containers.conf` can move
  it. Every branch of both checks, the harness's Docker path and a podman
  that will not answer at all included, was exercised against a stubbed
  engine before being believed.

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
- **A malformed CSRF cookie no longer breaks every write in the app.**
  `readCookie` ended in `decodeURIComponent`, which throws `URIError` on a
  value containing a stray `%`. The throw escaped the openapi-fetch request
  middleware *before* `fetch` was called, so `guard` in `endpoints.ts`
  normalised it to `ApiError(0, 'Could not reach the server.')`: every
  mutating request in the application reported a network outage that was not
  happening, no request was ever sent, and no amount of retrying could clear
  it — the user had to know to delete a cookie. The decode is now attempted
  and the raw value handed back when it fails.

  **Raw rather than absent, and the choice is the whole of the fix.** The
  alternatives were to return `null` — treat an undecodable cookie as no
  cookie — or to raise something the UI could name. Both are the client
  guessing at a question it cannot answer: only the server holds
  `SESSION_SECRET` and the session binding, so only the server can say
  whether a value is legitimate. Handing the raw bytes over sends the
  request, and the bytes are exactly what the browser puts in the `Cookie`
  header, so `require_csrf` compares like with like and answers 403 "CSRF
  validation failed" — a true message, surfaced verbatim by `describe`.
  Returning `null` would have reached the same 403 by a worse route, with
  the header omitted, a tampered cookie indistinguishable from the ordinary
  not-signed-in case, and nothing in the server's log to attribute the
  refusal to. It is the same reasoning that already sends a mutating request
  with no CSRF header rather than withholding it client-side.

  The two `it.fails` markers left by the coverage pass are ordinary passing
  tests now, and the request-level one gained the assertion that makes it
  discriminate: it pins the raw value arriving in `X-CSRF-Token`, where
  before it asserted only that *a* request was sent, which returning `null`
  would also have satisfied. Both were seen to fail against the unfixed
  module, and both catch a `null` fallback.

  **The vector this was reachable through is not closed**, which is why the
  entry is here and not under Security. The CSRF cookie is deliberately not
  `HttpOnly` — the frontend has to read it, that is the double-submit
  mechanism — and carries no `__Host-` prefix, so a sibling subdomain on the
  same registrable domain can still write one. What changes is the blast
  radius: an injected value used to wedge the client into a phantom offline
  state, and now produces a 403 that says what happened. `ROADMAP.md` records
  the prefix decision, which turned out to have a prerequisite rather than a
  cost, and folds the CSRF cookie into the OAuth state cookie's entry — they
  are one finding.

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
