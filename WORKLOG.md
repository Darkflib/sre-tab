# Worklog

Newest entries first. One entry per meaningful unit of work; note decisions
and deviations, not just activity.

## 2026-08-28 — Filing the deferred findings, and a gate that runs without a diff

No behaviour changed. This is bookkeeping, and the kind that stops being
bookkeeping the moment someone new reads the repository.

**The five absorbed security findings now live in the roadmap.** They were
recorded in the 18 August entry below and nowhere else, which is the wrong
place for them: a worklog entry is a record of a decision at a moment, and
these are standing conditions. The cost of leaving them there is paid twice
— the next reviewer re-reports them, and the assumption each one rests on
stays invisible, because nothing in `app/` says "this is acceptable because
there are three operators". Each is now filed under its own heading in
ROADMAP.md with the assumption that holds it open stated next to it, and
the section says plainly which event invalidates which item.

Two of the five were sharpened by re-reading the code rather than the
review. `upsert_user`'s race cannot produce a duplicate — `users.github_id`
is `unique=True`, so the loser takes an `IntegrityError` and the symptom is
a 500 on one callback, not two rows. And the superuser finding is not about
a role named `postgres`: `sre-tab-db.container` sets `POSTGRES_USER=sretab`,
which the official image creates *as* the cluster superuser, and the
application, the migration unit, and the backup all share one
`DATABASE_URL`. The precision matters because it is the difference between
a finding an operator can dismiss on sight and one they cannot.

**`failure.sh` and `success.sh` are gone.** They were the shell reproductions
that proved the backup-timer trap bug in d54800d, and they were committed to
the repository root, where they read as part of the deployment. The output
they produced is already quoted in the 18 August entry, which is the durable
form; the scripts themselves were scaffolding.

**Both workflows now run weekly and on demand.** The gate was wired to
`pull_request` and `push` only, which means it reported on the diff and said
nothing about the world. That is the wrong shape for a project that goes
weeks between commits: `audit` fails on a CVE published against a dependency
that was clean at merge time, `container` builds from a base image that
moves, and `Docs` executes a quickstart against software this repository
does not control. None of those needs a commit to break, and all of them
currently break silently until someone next needs to ship.

Two details were not free. The concurrency group is now keyed on
`github.event_name` as well as the ref — without that, a Monday run on
`main` shares a group with a push to `main` and `cancel-in-progress` lets
the timer cancel a publish. And the schedule has a failure mode that lands
precisely when it is most needed: GitHub disables scheduled workflows after
60 days of repository inactivity, by email rather than by failing. Sixty
quiet days turns this off. That is written down in CONTRIBUTING.md rather
than left to be discovered, because the alternative is believing a check is
running when it is not, which is the same class of mistake as the green
Semgrep gate that had scanned nothing.

No job `name:` changed, so the eight required status checks are untouched —
the trap CONTRIBUTING.md documents at length.

**One stale claim in the README, found while checking the rest.** "Known
gaps" said nothing covered the feed or the filters, which stopped being true
when the 73 filter and volume tests landed. Corrected to name what is
actually uncovered — `src/api/client.ts`, `usePagedResource`, and every
component and route, all of which need a DOM and a mocked `fetch`.

## 2026-08-18 — An external review, measured rather than believed

Codex reviewed the tree and returned three High findings, twelve Medium,
and four lower. Every one was checked against the code and against what
this deployment actually is — single instance, three allow-listed
operators, an operator-curated source catalogue with no route that adds a
feed. Most of the review survives that check. Two claims did not, and one
finding turned out to be considerably worse than it was filed as.

**The reused `HTTPException` is not a leak, it is an unauthenticated
denial of service.** `app/api/deps.py` and `app/api/v1/auth.py` each held
a module-global exception instance and raised it on every failure. Python
appends a frame to `__traceback__` on each raise, and the object is a
module global, so nothing ever collects it. The review reported traceback
depths of 9, 18, 27, 36, and 45 across five requests; that reproduces
exactly. What it did not do was carry the number forward:

    2000 unauthenticated 401s
      traceback depth: 18009 frames
      RSS growth:      65.4 MB  (32,719 bytes/request)

Each retained frame pins the `Request`, the raw token, and the
SQLAlchemy `Session`. Against `MemoryMax=768M` that is roughly 23,000
requests to an OOM, on `/api/v1/me`, which needs no credentials and has
no rate limiter — the two limiters cover the auth routes only.
`Restart=always` makes it a crash loop rather than a kill, which is a
distinction worth exactly as much as it sounds.

**Two corrections to how the review framed that one, both in the
direction of precision.** The session tokens retained via `deps.py` are
invalid by construction: `_UNAUTHENTICATED` only raises once resolution
has already failed, so "retains credentials" is not true of that half.
The real credential retention is the other instance — `github_callback`
raises the shared 429 at the top of the function, where `code` and
`state` are already bound, so genuine OAuth codes were being pinned in
frame locals. Narrow, since the failure limiter has to have tripped
first, but it sits directly against the AGENTS.md rule about never
logging OAuth codes, and for the same reason.

**The parser cap does not cap the parser.** `assert_safe_document` parses
the whole document with defusedxml, `feedparser.parse` parses it again,
and only then does `[:MAX_ENTRIES]` apply. A valid RSS document built to
sit just under `source_fetch_max_bytes`:

    feed: 5.24 MB, 91978 entries (MAX_ENTRIES=500)
    RSS peak after parse: +97 MB, 2.27s, kept 500 entries

The review measured 197 MB from a denser document; the ratio is the
finding either way. `refresh_all` is a serial list comprehension, so this
is one hostile or compromised upstream against a 768 MB ceiling.

**DNS resolution sits outside the fetch deadline.** `fetch()` starts a
deadline, then calls `UrlGuard.validate()`, which calls `getaddrinfo()`
with no bound, before anything checks the clock. The review called this
"indefinitely", which is not right — glibc bounds it by `resolv.conf`,
typically ten to forty seconds — but outside a deadline and in front of a
serial loop is enough.

**One finding was simply stale.** "Generated API types have no drift
gate" is answered by the entry two below this one: `tests/test_openapi.py`
compares the committed document against the live schema byte for byte,
and `ci.yml` regenerates `schema.d.ts` and diffs it, with a
`git ls-files --error-unmatch` guard precisely because an untracked file
would otherwise diff clean. That landed in d608291. The review read
`package.json` and stopped there, which is a fair way to be wrong and
still worth recording: a gate that lives in CI is invisible to anyone
reading the package manifest for it.

**What the security model genuinely does absorb**, and which is therefore
filed rather than fixed. The OAuth state cookie can be tossed from a
sibling subdomain, but the exploit needs the attacker's own GitHub ID on
the allow-list, which makes it inert at three operators; `__Host-` is
also not free, since it mandates `Path=/` and would trade the current
`/api/v1/auth` scoping for domain integrity. Feed image URLs get a weaker
check than item URLs — no IP-literal rejection, no dot requirement — and
`img-src https:` lets the browser fetch them, but sources are CLI-curated,
the request is blind, and `referrerPolicy="no-referrer"` is already set.
The application connects as the PostgreSQL superuser, which upgrades an
application compromise to `COPY … PROGRAM`; that one is real and is the
only deferred item with real cost, because role separation touches
migrations, restore, and the smoke test. Unpruned sessions and the
`upsert_user` insert race are both true and both negligible at three
users.

**Fixed here**, in the order the fix is small and the security model
gives nothing back: fresh exceptions per raise, with a regression test
that asserts traceback depth stays flat; the backup timer stopped for the
duration of a restore, which it was not; `|| true` off the setuid strip,
which was the fail-open guarding the thing that stands in for
`NoNewPrivileges`; the tested image carried through to `publish` so the
signed bytes are the bytes that passed the smoke test; the parser bounded
before expansion; DNS bounded by the deadline; HSTS, which was nowhere at
all — not in the middleware, not in the Caddyfile mirror, not in the list
of things `deploy/README.md` tells the outer proxy it must not get wrong;
and the deploy template's allow-list emptied, since it shipped three real
GitHub accounts directly underneath a banner explaining that empty is the
correct fail-closed default.

**The backup-timer fix was itself wrong, and review caught it.** A trap on
`INT`/`TERM` runs its handler and then carries on from where the signal
landed — it does not exit. Sharing one handler with `EXIT`, which is what
the first version did, therefore released the timer and continued
straight into `DROP DATABASE` with the schedule live again, ran the
handler a second time on the way out, and returned 0. Ctrl-C during a
restore was the one input that reopened the window the pause exists to
close, and it reported success while doing it:

    === first version ===        === after ===
    paused timer                 paused timer
    TIMER RESTARTED              TIMER LEFT STOPPED (restore incomplete)
    REACHED DROP DATABASE        exit=130
    TIMER RESTARTED
    exit=0

Fixing that surfaced a second one nobody had reported, and broader: the
`EXIT` trap re-armed the timer on *every* exit, so a failed `pg_restore`
also handed the next scheduled backup an empty database. Releasing the
schedule is now conditional on the restore having been verified, and when
it has not been the timer is left stopped with an instruction rather than
silently — a schedule disabled forever being the other way to get this
wrong. Worth recording that the guard against "a backup of an empty
database" shipped, in its first form, with two paths that produced
exactly that.

**And the parser bound had a hole, found by probing the fix rather than
the original.** Counting nodes bounds memory and says nothing about
time. feedparser is quadratic in the attribute count of a *single*
element, so a document with four elements and 380,000 attributes on one
of them sailed through a ceiling built for a million tiny tags: 20,000
attributes is 0.21 MB and 2.27s, 60,000 is 0.65 MB and 21.3s, and the
5 MiB version ran for minutes. An eighth of a permitted body stopping a
serial refresh cycle for twenty seconds is a better attack than the one
that started this. The streaming gate reaches the same 60,000 attributes
in 0.03s, which is what makes it the right place to refuse them — the
expense is entirely downstream. Capped at 256 per element, far past
anything real, and the 5 MiB bomb now stops in 0.24s.

The general lesson is the one this repository keeps relearning in
different clothes: a limit bounds the quantity it counts and nothing
else. `MAX_ENTRIES` bounded rows and not the parse; the node ceiling
that replaced it bounded allocation and not time. Both looked like
"the parser is bounded" from the outside.

**Then review found a third version of the same mistake, in the same
function.** The counters ran on `end` events, and end events arrive
innermost-first — so a document that only nests produces none at all
until it has stopped descending. 200,000 nested tags is 1.40 MB, inside
every byte limit here, and the first `end` arrived after 200,002
`start` events with 57 MB already allocated and the ceiling not
consulted once. Counting moved to `start`, which is also where expat
delivers a tag's attributes, so both counts are now taken at the
earliest point either can be known. The same document is refused in
0.05s.

Three rounds on one function, each fixing a bound that was real and
measured, and each leaving another quantity unbounded. Worth writing
down as the shape rather than the incident: "bounded" is not a property
a parser has, it is a property of one quantity at a time.

**And the DNS fix held the process open at shutdown.** CPython joins
live `ThreadPoolExecutor` workers during interpreter shutdown, so an
abandoned lookup kept the whole process alive until the resolver gave
up: a test file pytest reported as taking 0.25s took **31 seconds** of
wall clock, and in production the same mechanism would have delayed
every SIGTERM arriving while a resolver was wedged — bounding the
request path by leaking the cost onto the shutdown path. A bare daemon
thread is discarded at exit instead, and one per lookup is affordable
because the refresh loop is serial. Same file, 1.1s. The regression test
runs a subprocess and times its exit, because "does the process
actually exit" is the claim and nothing about inspecting the thread
would have said so.

**Two more from the same round, both fair.** The empty allow-list still
shipped three real GitHub IDs in a comment directly above it, which is
an identity somebody pastes into a live deployment without meaning to;
they are placeholders now, and no real account is named in the template
at all. And `release_backup_timer` ended in `|| true` — the same
fail-open this branch removed from the Containerfile, reintroduced by
me four files away. A restore would have reported success over a host
whose backup schedule it had switched off and never switched back on.

## 2026-08-18 — The deploy documents, now executed by CI too

The harness half, after the hand-run below. `docs.yml` gained a
`deploy-procedures` job that runs seven `docs:run` blocks out of
`deploy/README.md` — preparation, configuration, secrets, first start,
verification, network replacement, and an assertion that the recreated
range starts above Caddy's pinned `.20`.

**The roadmap was right that the runner was the expensive part, and
wrong about why — and I was wrong to say it had been wrong.** The entry
said a runner with systemd was the obstacle. `ubuntu-latest` has systemd,
root, and podman a package away, so the job got written and the roadmap
got a line saying the cost had been misplaced. Then the job failed:

    [conmon:e]: Include journald in compilation path to log to systemd journal
    Error: conmon failed: exit status 1
    sre-tab-web.service: Main process exited, code=exited, status=126

Ubuntu's `conmon` is built without journald support, and the three
long-running units set `LogDriver=journald` on purpose — the Operations
section argues for it, because `podman logs` is worth having on those
three. So the stack cannot start on a GitHub runner at all, and nothing
downstream of that was ever going to be tested.

Making it pass would mean overriding `LogDriver` for CI, which is testing
a deployment other than the one that ships. A green gate over the wrong
artefact is worse than no gate — that is the same argument the semgrep
`p/bash` guard exists for — so the job came out and the error text stays
in `docs.yml` where the next person to write it will find it first.

The lesson is narrower than "CI cannot run this": it is that "the
runner has systemd" and "the runner can run these units" are different
claims, and only the first one is easy to check. Closing it needs a
runner whose conmon has journald.

The harness itself is real and stays: seven blocks, exit 0 end to end on
Debian 13 with podman 5.4.2, and the command is in CONTRIBUTING.md. The
other half of the original cost was two documented commands that cannot
run as written — `sudoedit`, and a client secret written as
`/path/to/github-client-secret`.

Both were fixed by naming the input rather than scaffolding around it.
The path became `${GITHUB_CLIENT_SECRET_FILE:?}` — a variable holding a
*path*, so the document's own rule that argv never carries the secret
survives intact — and the configuration section documents the
non-interactive edit next to `sudoedit`, which is what a
configuration-management run does anyway. The test of whether bending a
document toward execution is legitimate is whether the result reads
better to a human, and `/path/to/…` was always a substitution the reader
had to make silently.

**The verification block changed for a reason from the entry below.** It
polled instead of requesting once, because `systemctl` returning is not
the all-clear. The document had been telling an operator to run a check
that fails a good fraction of the time — which is how the deploy-window
finding was made in the first place.

**The harness found something on its first run.**
`create-secrets.sh` refuses when `sre-tab-postgres-password` exists,
because a new password is one the existing database does not have — good
behaviour, and its own usage text said the opposite: "Existing secrets
are replaced." That sentence described the `podman secret create
--replace` call inside the helper rather than what the script does before
reaching it. Corrected.

Then it found the same thing again, about me. Clearing the secrets by
hand and re-running produced a failed migration unit: the regenerated
password did not match a database volume initialised with the old one,
which is precisely the state the guard exists to prevent and which I had
walked around to get a clean run. A genuinely clean host means secrets,
volumes, *and* containers. Recorded on the roadmap, because the next
person to reproduce this will make the same mistake in the same order.

One thing improved in the runner itself: it labelled log groups with the
document's basename, so two different `README.md` files both appeared as
`README.md:58`. It uses the path now.

**And one found by watching a run stall.** `apt-get install podman` in
the container job sat for eight minutes against a normal thirty seconds,
and nothing in `ci.yml` bounded it: not one of its seven jobs set
`timeout-minutes`, while both jobs in `docs.yml` do. GitHub's default is
360 minutes, so a hung network step burns six hours of runner before
anyone gets a red cross — and on a pull request it reads as "still
running" the whole time, which is the same shape as the branch-protection
rule that failed safe by never reporting. All seven now carry a budget:
15 minutes for the fast jobs, 30 for the two that build images.

## 2026-08-17 — The deploy documents, executed on a real host

A second Debian 13 host with podman 5.4.2, and a hand-run of the four
`deploy/README.md` sequences that nothing has ever executed: host
preparation, secrets, first start, and network replacement. Hand-run
rather than harnessed first, on the reasoning that a real host is the
scarce resource and the extraction harness can be written anywhere
afterwards.

**Four of the four procedures are correct as written**, which is worth
saying as plainly as the failures. `install.sh` is idempotent, seeds
`app.env` once and preserves it while replacing everything else,
and creates `/srv/sre-tab/backups` `999:999` mode `0700` — with gid 999
confirmed as `systemd-journal` on the host the claim is about, which is
the whole reason for the mode. `create-secrets.sh` takes the secret on
stdin and the documented invariant holds: the password inside
`sre-tab-database-url` matches `sre-tab-postgres-password`. First start
resolves the ordering correctly from cold. The network-replacement
sequence works including its subtle claim — with the network removed,
`sre-tab-network.service` still reports `active`, which is exactly why
the document says to use `podman network rm` rather than stopping the
unit — and the recreated network puts the range at `.32–.254`, leaving
Caddy's pinned `.20` outside the pool, so the Phase 3 collision fix is
intact. `systemctl --failed` stayed empty throughout, which is the
`NoNewPrivileges` reasoning holding somewhere other than where it was
written.

**The one wrong claim was the deploy window, and the error was in the
mechanism rather than the number.** "A sub-second blip while Caddy
restarts" measured 43.7 seconds. The application was never the slow
part: it stops answering for 0.5s and is serving again 2.8s in.
`Notify=healthy` gates on the image's healthcheck, whose first run comes
one whole interval after start whatever `--start-period` says, and the
interval was 30s — so systemd waited 32s for a unit that was ready in
under three, holding Caddy down behind it because Caddy is ordered after
the application.

The interval had drifted for a structural reason worth recording:
`sre-tab-db.container` has carried `HealthInterval=10s` in its unit all
along, while the application's lives in the image. Two definitions in two
files, and only one of them got tuned. Bringing the image to 10s cuts the
`systemctl` wait from 35.6s to 15.4s, and was verified by building the
image on the host and re-measuring rather than by reasoning about it.

**It did not fix the outage, and that is the more useful finding.**
Total unreachability went only 43.7s → 36.1s, because roughly 20s of it
happens *after* `systemctl` returns — and that portion grew as the
healthcheck wait shrank. During it a TCP connect to the published port is
**black-holed rather than refused**, while Caddy's own log shows it
serving 50ms after its container starts. So it is neither Caddy booting
nor the application: something between the published port and the
container is not carrying traffic yet. Not root-caused, and written down
as unexplained rather than guessed at.

**The obvious follow-up measurement was malformed, and finding that out
improved the finding.** The tail was first written up hedged: polling came
from the host's own loopback, so perhaps an off-host client would not see
it. There are no off-host clients. `sre-tab-web.container` publishes
`127.0.0.1:8080:8080` deliberately, because TLS terminates at the host's
existing proxy — which reaches Caddy over loopback, exactly as the polling
did. The hedge was backwards: nothing insulates users from this, it
reaches them as 502s. Opening the port to test it would have measured a
topology this deployment does not have.

Chasing the mechanism on the host instead was the right spend, and it is
not conntrack, which was the first guess. Two things serve
`127.0.0.1:8080`: netavark's hostport DNAT rule to Caddy's pinned
`10.89.61.20`, and a *reservation* listener podman holds on the same port
so nothing else claims it — `conmon`, in `ss -lntp`. Probing across a
restart walks all three states in order: `refused` while both are gone,
then **accepted and then hung**, then serving. The middle state is the
tail: the reservation listener takes the connection and never forwards
it, because the DNAT rule is not back yet. That is why it reads as
black-holing, and why none of the three services logs anything about it.
Whether the ordering is fixable from the unit files or is podman's to fix
is still open — and since `PublishPort=127.0.0.1:8080:8080` is the same
line orbit-data uses, whatever the answer is applies to both.

The remaining limit is ordinary: a 4-core cloud instance, so the absolute
numbers are that host's.

The operational conclusion survives every one of those caveats, and is
now in the document: a deploy is not over when `systemctl` returns, so
wait for `healthz` rather than the prompt. The documented verification
step run immediately after a restart fails — which is how this was found,
because it failed on me.

One contradiction fell out alongside. The Containerfile's header claimed
`sre-tab.container` sets `HealthCmd=` explicitly and that the deployment
therefore did not depend on the image's `HEALTHCHECK` surviving an
OCI-format build. The unit sets no `HealthCmd=`, deliberately, and its own
comment and CI's both say so. The dependency is total, and the comment
was reassurance pointing the wrong way.

## 2026-08-17 — The contract, gated at both links

The roadmap's cheap version of the static-OpenAPI item: CI asserting the
live schema matches the reviewed committed file. It came out smaller than
the entry costed it and one link larger than the entry described, and both
differences are the interesting part.

**Smaller, because a drift check is not a serving change.** The item was
parked on ownership across three files — `app/main.py` is frozen Phase 0
property, `deploy/Caddyfile` is the only place the served artefact could
be decoupled from the live app, and the committed artefact lives in
`frontend/openapi.json`. The check touched neither of the contentious
two. What the committed document matches the served schema is a property
of the application, so it belongs in the test suite, next to the
assertions about the endpoint table that were already there. No CI wiring
at all for that half.

**One link larger, because there are two committed artefacts, not one.**
`src/api/schema.d.ts` is generated from `openapi.json`, so the contract
reaches the client through a two-step chain, and frontend/README.md held
it together with "regenerate both files in one commit". That sentence was
the entire enforcement mechanism.

The second link is the one worth having. Skip the first regeneration and
the committed document is visibly stale. Skip the second and nothing
looks wrong anywhere: the types stay internally consistent with a
document that has stopped describing the server, so `tsc` passes —
faithfully, against the wrong contract. Neither the typecheck nor the
Python suite could have caught it, because each is correct about the
thing it can see.

Split by toolchain, so each link is checked where the toolchain for it
already exists: `tests/test_openapi.py` for the document, a step in the
`frontend` job for the types. Neither job gained a dependency and no new
job was created — deliberate, a week after learning that protection lives
in GitHub's settings and not in a file anyone reviews.

**Avoiding the protection change entirely was the wrong instinct, though,
and it took saying it out loud to see why.** The job was still called
`Frontend lint, types, tests, build` while doing something the name did
not mention, which is the same least-surprise argument the last three
fixes turned on, pointed at whoever next reads a red cross and tries to
work out what failed. Keeping a stale name to dodge a settings edit
optimises for the person making the change over the person debugging it.

So it is `Frontend lint, types, contract, tests, build` now, and the
required context was updated in the same change, following the procedure
CONTRIBUTING.md was given for exactly this. Order matters and is worth
recording: rename, push, let the new context report, *then* rewrite the
required set. Protection then never names a context that has not
reported, and the only intermediate state is a pull request waiting —
which is the safe direction, and the one the broken rule already proved
the repository survives. Verified afterwards by set-differencing the
required contexts against the reported ones, not by rereading the rule.

Doing that turned up a limitation in the documented verification command
itself. It set-differences the required contexts against the check-runs
on `origin/main`, and while a rename is unmerged `main` has not reported
the new context — so the command lists it and reads exactly like the
failure it exists to detect. Comparing against the pull request's head
instead is empty, which is the true answer; both are now in
CONTRIBUTING.md, along with why the ordering has to be this way round.
Rewriting the rule first would leave the required set naming a check
nothing had ever reported, which is indistinguishable from the broken
rule until CI next runs.

Measured end to end on #5: all eight required contexts reported and
passed under the new name, and the request came back `MERGEABLE`.
`mergeStateStatus` was `UNSTABLE` again, and again it is CodeRabbit
posting a check outside the required set rather than a protection
failure.

Two smaller things fell out. The old name appeared in CONTRIBUTING.md
three times, once as a third copy of the whole context table inside the
narrative about the broken rule; that copy is now a pointer to the table,
because a list repeated three times is two opportunities to drift. And
the rename is annotated in `ci.yml` itself, next to the `name:` — the one
place someone editing it will actually be looking.

Both were green on the first run, which is the outcome to want: the gate
pins a property that is true rather than repairing one that is not.
Mutation-tested on the theme suite's precedent, one per link — a field
added to `HealthResponse` with no regeneration fails the document test
and, tellingly, fails nothing else, because the existing endpoint-table
assertion is about which operations exist and not about their shape; a
regenerated `openapi.json` with stale types fails the CI step. The first
mutation attempt was malformed and worth recording: hand-editing
`schema.d.ts` and then running the check passes, because regeneration
simply overwrites the edit. That is correct behaviour and a bad test of
it — the check is for a stale *input*, not a tampered output.

**Review found two holes in the gate, and one of them was the gate's own
failure mode.** Both bot reviewers independently flagged that `git diff`
reports tracked files only, so absence reads as agreement: delete the
committed `schema.d.ts` and the step regenerates it as an untracked file,
diffs nothing, and goes green — and `tsc` passes too, against the copy
just written, while a clean checkout would have neither. Reproduced
before fixing, and the reproduction is the part worth keeping: the job
went entirely green on a tree that does not build. That is the semgrep
`p/bash` failure again in a different costume — a gate that checked
nothing while looking like coverage — which makes it a poor thing to
have shipped in a change whose entire subject is gates that check
nothing. `git ls-files --error-unmatch` now asserts the file is tracked
before the diff is trusted.

The second is smaller and was my claim rather than my code.
`Path.read_text` applies universal-newline translation, so it decodes a
CRLF file to the same string as an LF one and reports them equal — while
the test, this worklog, and CONTRIBUTING.md all said "byte for byte".
There is no `.gitattributes` pinning line endings here, so a checkout
with `core.autocrlf` is exactly how that arises. Comparing `read_bytes()`
makes the stated property true rather than nearly true. Verified by
rewriting the committed document with CRLF endings and watching the test
fail, which it did not before.

One documentation claim was falsified in passing. CONTRIBUTING.md said
`npm run check` was the local equivalent of the `frontend` job, and it no
longer is: the job now regenerates and diffs, and `check` deliberately
does not, because a command called `check` should not write to the
working tree. Amended rather than papered over, along with the asymmetry
it creates — a response-model change fails locally under `uv run pytest`,
while forgetting `generate:api` is caught only in CI.

The serving change itself stays on the roadmap. It now has a trustworthy
artefact to serve, which is the prerequisite it was actually waiting on.

## 2026-08-17 — Least surprise, applied to two audiences

Three fixes that came out of the filter-model work below, decided by
asking who is surprised and where.

**An empty selection is a step, not a destination.** "Save as my default"
wrote the resolved chip state into preferences, so deselecting every
source and saving stored an empty saved selection — which the server
reads as "no preference, use the instance defaults". Two clicks turned
"show me nothing" into "show me everything", and specifically into the
state `preferences.py`'s default-selection comment exists to argue
against, where general news drowns the low-volume sources the product is
for.

The store has no way to represent "my default is nothing", so the honest
answer was to stop offering it rather than to make it representable. That
was considered — a sentinel, or an explicit "has a selection" flag — and
rejected: a schema change and a server-logic ripple to persist a state
whose only value is as a transient editing step. You clear the chips so
you can pick two, not so you can keep none. The control is disabled while
nothing is selected, with the reason stated in the filter bar; a dead
button with no explanation is its own small surprise.

**A second fault shared the root and is fixed with it.** `saveAsDefault`
wrote *both* dimensions from `effectiveSelection`, which resolves an
un-overridden dimension into the whole catalogue so chips can render.
Writing that back converts "follow the instance" into a pinned snapshot
of today's catalogue, after which a source added later never appears for
that user and nothing says why. It only bites once saved preferences are
empty — which is what the inversion above caused, so the two compounded:
invert, then freeze. The patch now carries only the dimensions the user
actually overrode.

The generalisation is the part worth keeping. `effectiveSelection`
returns a **display** value, lossy by design because it resolves *unset*
into a concrete list for rendering. Persisting a display value into a
store with a different vocabulary is what inverted the meaning. Persist
intent, not appearance.

**The slug question resolved the other way, because the audience is
different.** That surprise belongs to an operator at a terminal:
`sre-tab sources add --slug 'a,b'` succeeded, and the consequence
appeared later, in another component, as a source that lists correctly
and filters to nothing. For a CLI, least surprise means failing at the
point of the mistake — the same trade `validate_feed_url` already makes
one field along, and the same conclusion the SSRF work reached about
config-time validation. So the fix is enforcement at creation rather than
tolerance in the client: `add_source` and `add_topic` refuse anything but
lower-case alphanumerics with single hyphens, reusing the pattern that
already guarded the Medium tag, and `sre-tab status` reports rows that
predate the check and exits non-zero. Existing slugs are reported rather
than migrated, because rewriting one in place breaks every saved
selection naming it.

Writing that down turned up a dialect divergence: `medium_source` capped
the *tag* at 64 characters and then prefixed `medium-`, so a 60-character
tag made a 67-character slug — accepted by SQLite in development and
refused by PostgreSQL, where `String(64)` is real. The composed slug is
checked now, not just the tag.

The client is left fragile on purpose, and the `it.fails` marker stays to
say so. Its comma limitation is contained by a constraint held somewhere
else entirely, and that is exactly the kind of thing a reader needs told
rather than left to infer — which is the mistake the entry below records.

## 2026-08-17 — The filter model under test, and what it exposed

73 Vitest tests over `src/feed/filters.ts` and `src/feed/volume.ts`, the
cheap half of the frontend-coverage item and the half the roadmap said to
take first. It was right about the ordering for the stated reason — both
modules import types only, so they needed no DOM, no request mocking, and
no new dependency — and the suite goes from 114 to 187 tests still running
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
