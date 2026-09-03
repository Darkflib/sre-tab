# Contributing

The project is small, self-hosted, and opinionated about a handful of things.
This document is the short version of what those are, so a pull request does
not discover them from a failing check.

Start with [README.md](README.md) for what the project is and how to get it
running, and [prd-v1.md](prd-v1.md) for what v1 does and deliberately does
not do. If a change is on the v1 non-goals list, or on
[ROADMAP.md](ROADMAP.md) as deferred, say in the pull request why the timing
has changed rather than assuming nobody meant it.

## Getting set up

The [quickstart](README.md#quickstart) is executed on every push, so it works
from a clean clone. Once it does, add the git hooks:

```sh
uv run pre-commit install
```

The hooks are all local — they run out of the `uv`-locked virtualenv rather
than pulling their own copies of Ruff and mypy, so a hook can never be a
different version from the one CI uses.

## The gate

Every one of these has to pass before a commit:

```sh
uv run ruff format --check .
uv run ruff check .
uv run mypy .                          # strict, and it means it
uv run pytest
uv run bandit -c pyproject.toml -r app
```

and for the client:

```sh
cd frontend && npm run check           # eslint, tsc, vitest, vite build
```

`npm test` is `vitest run` — one shot, not the watcher; `npm run test:watch`
is the watcher. CI runs all four as separate steps, and one thing more: it
regenerates `src/api/schema.d.ts` and fails on a diff. `npm run check` does
not, deliberately — a command called `check` should not write to the working
tree — so it is the local equivalent of the `frontend` job in everything but
that step.

Four further checks run in CI and are worth knowing about before a change
that touches their subject matter:

| Command | Fails when |
| --- | --- |
| `deploy/scripts/check-env-example.sh` | a setting exists in `app/settings.py` and not in `.env.example` |
| `deploy/scripts/check-header-parity.sh` | the security headers in `app/middleware.py` and `deploy/Caddyfile` disagree |
| `cd frontend && npm run generate:api && git diff --exit-code` | `src/api/schema.d.ts` is older than the `openapi.json` it is generated from |
| `SRE_TAB_POSTGRES_URL=... uv run pytest tests/postgres` | a PostgreSQL-only path breaks |

The API contract reaches the client through two committed artefacts, and
each is checked where the toolchain for it already exists. `uv run pytest`
covers the first — `tests/test_openapi.py` compares `frontend/openapi.json`
against the schema the application serves, byte for byte. The table's third
row covers the second. Change a response model without regenerating and the
first fails locally; regenerate `openapi.json` without regenerating the types
and only CI notices, which is the asymmetry to keep in mind.

`tests/postgres/` skips silently without `SRE_TAB_POSTGRES_URL`, which is why
CI supplies one. The three things it reaches that SQLite cannot are
`pg_try_advisory_lock` (the scheduler's leader strategy), the PostgreSQL
`ON CONFLICT` branches, and the migration running on the engine it will
actually run on. Point it at any throwaway database:

```sh
SRE_TAB_POSTGRES_URL=postgresql+psycopg://sretab:sretab@127.0.0.1:5432/sretab \
  uv run pytest tests/postgres -v
```

The full deployment smoke test — fresh PostgreSQL, migrations, health checks,
a real backup, and a real restore through the same scripts an operator would
run — is engine-agnostic. On a developer machine with Docker:

```sh
docker build --file Containerfile --tag sre-tab:dev .
CONTAINER_ENGINE=docker SRE_TAB_IMAGE=sre-tab:dev deploy/scripts/smoke.sh
```

CI does the same under Podman, where the build needs `--format docker`: OCI
format has no `HEALTHCHECK` field, and `sre-tab.container`'s `Notify=healthy`
waits on the image's healthcheck, so an OCI-format build hangs the unit until
`TimeoutStartSec` instead of failing fast.

Run it on a host that is not already running the stack. It uses the real
container names, deliberately, so that the Caddyfile's upstream is tested
verbatim rather than against a renamed copy.

### When CI runs, and why that is not only on a diff

Both workflows run on every pull request, on every push to `main`, on demand
via `workflow_dispatch`, and weekly — `CI` at 06:17 UTC on Mondays, `Docs` at
06:41. `CI` also runs on a push of any `v*` tag, which is how a release is
built; `Docs` does not, because a tag names a commit that has already been
through it on the way to `main`.

A tag build runs the identical `needs:` chain — `python`, `postgres`,
`audit`, `sast`, `frontend`, `container` — so a release is gated by the same
six jobs as a merge, not by a shorter path of its own.

The schedule is not belt-and-braces. Several of these jobs answer questions
whose answer changes with no commit behind it: `audit` fails when a CVE is
published against a dependency that was clean yesterday, `container` builds
from a base image that moves, `publish`'s verification depends on a registry
and on cosign's roots, and the quickstart in `Docs` executes commands against
software this repository does not control. On a project that goes weeks
between commits, a gate wired only to pushes reports on the diff and stays
silent about the world, and the first news of the drift arrives when someone
needs to ship.

A scheduled run does everything a push to `main` does except publish: the
three `if: github.event_name == 'push' && (github.ref == 'refs/heads/main' ||
startsWith(github.ref, 'refs/tags/v'))` guards in the `container` and
`publish` jobs mean a Monday run builds the image and smoke-tests it without
signing or pushing anything. They are one decision written three times, and
have to stay identical: the two in `container` export the tested image, and
the one on `publish` consumes it, so a build that satisfies one pair and not
the other runs the whole gate and then fails at `download-artifact`.

The concurrency group is keyed on the event as well as the ref, so a
scheduled run cannot cancel a publish in flight — and neither can a push to
`main` cancel a tag build, or the reverse, because `refs/heads/main` and
`refs/tags/v1.1.0` are different groups. That matters more than it sounds:
tagging a release and merging the next change are often minutes apart, and
`cancel-in-progress` would otherwise kill a release build that had already
pushed an image.

One caveat that is worth knowing precisely because it fires under exactly the
conditions the schedule exists for: **GitHub disables scheduled workflows on
a repository with 60 days of no activity.** It emails the owner and stops
running them; it does not fail. Sixty quiet days therefore silently removes
this, and re-enabling it is a manual step in the Actions tab.

## Documentation is executed, not proofread

`.github/workflows/docs.yml` runs the README's quickstart on a clean checkout
on every push. This is not decoration. Several documented procedures here have
been wrong while reading perfectly: `install.sh --start` never recreated a
removed network, the upgrade procedure was wrong as written, and
`deploy/README.md` described a deploy as a "sub-second blip" that measured
43.7 seconds. All were found by running them on Linux, not by reviewing them.

<a id="diagrams-parse-but-are-not-read"></a>
### A diagram that parses is not a diagram that reads

`.github/scripts/check-mermaid.mjs` parses every ```` ```mermaid ```` block in
the tracked Markdown and runs in the `Docs` workflow. It exists because an
unparseable block is not a wrong word, it is a red "Unable to render rich
display" box where the diagram should be — on `ARCHITECTURE.md`, which is a
reasonable guess at the first page a newcomer opens — and nothing else here
would notice. A reviewer would not either: the diff shows the source, and
Mermaid source that is rejected by the grammar looks like Mermaid source that
is not.

**It is a syntax gate and only a syntax gate, and the gap is not small.** All
nine diagrams in `ARCHITECTURE.md` parsed on the first attempt. Four of them
were still wrong when rendered and looked at, and the worst was wrong in the
way that matters: the layout engine routed one edge through an unrelated node,
so the picture showed an arrow between two services that never talk to each
other. Valid source, false diagram, and no checker will ever catch it. Render
it and look:

```sh
npx --yes @mermaid-js/mermaid-cli -i diagram.mmd -o diagram.png
```

**The pinned parser is not GitHub's, and cannot be.** GitHub does not publish
the Mermaid version it renders with, and moves it on its own schedule, so this
gate agrees with the real renderer only approximately — syntax newer than
whatever GitHub is running would pass here and still render as that red box.
The only published way to ask is to put a fenced block containing the single
word `info` in any Markdown on github.com and read what it renders. Worth
doing if a diagram is rejected there and accepted here.

**The parser is a second npm project, and that is deliberate.**
`.github/scripts/package.json` and its committed lockfile exist so the gate
runs `npm ci` against the same 122 packages every time rather than whatever
resolved that morning — the script imports and executes that code, so an
unpinned transitive graph is a real gap and not a theoretical one. To change
the parser version, edit the manifest and run `npm install` in that directory;
`npm ci` refuses a manifest and lockfile that disagree, which is what makes
forgetting the second step loud. It is kept out of `frontend/package-lock.json`
and out of `uv.lock` on purpose: a documentation linter has no business in the
client's dependency tree or in the image, and Renovate manages it through its
ordinary npm manager with no custom rule needed.

happy-dom appears in two manifests — here and in the frontend, which vets it
for the four Vitest files that need a document. They pin it independently,
Renovate moves both in the same weekly group, and they are not required to
agree; reusing it is about not vetting a second DOM implementation, not about
the versions matching.

Two things the script does that are not obvious, both from the rule in
[AGENTS.md](AGENTS.md) about what a green check is worth. **Zero blocks is a
failure**, because a repository with no diagrams and an extractor that has
stopped finding them print the same success line — if the diagrams are ever
all deleted, deleting the check is the honest response. And it **self-tests
before it reports**: a known-good diagram must parse and a known-bad one must
not, or it exits without saying anything about the corpus at all. That second
one is not theoretical. Mermaid loads DOMPurify at parse time and needs a
`window`; run it without one and it does not fail cleanly, it fails
*partially* — seven of the nine diagrams error and two parse anyway, which
from the outside is indistinguishable from seven broken diagrams.

### Linking to a heading requires an explicit anchor

A link into the middle of a document has two halves and they rot at different
rates. The path half breaks when a file moves, which is rare. The fragment
half breaks when a heading is reworded, which happens here constantly — every
roadmap entry that lands gets its heading rewritten — and it breaks *quietly*,
because GitHub still serves the page and simply ignores an anchor it does not
recognise. The reader lands at the top of a 30KB document and has no way to
know they were sent somewhere specific.

So a fragment must name an anchor the target document declares, in exactly
this shape:

```markdown
<a id="branch-protection"></a>
## Branch protection
```

Column zero, its own line, immediately above the heading, and unique within
the file. `python3 .github/scripts/check-doc-links.py` enforces all of it and
runs in the `Docs` workflow.

**The anchors are declared rather than computed, and that is the whole
point.** GitHub derives a heading's anchor with an algorithm that is not a
documented contract, and the obvious reimplementation of it is wrong:
measured against GitHub's own render of this repository,
`## 2026-08-17 — Phase 0 foundation` becomes `#2026-08-17--phase-0-foundation`
— the em-dash is stripped and each space around it becomes its own hyphen,
because whitespace is replaced one-for-one rather than collapsed. A checker
that collapsed it would agree with the wrong link and pass it. That is a
false pass, and a false pass is the specific way this repository keeps
getting hurt. Declaring the anchor makes the check an exact string match
against something we own.

Two consequences worth having on purpose. Adding these breaks nothing, because
GitHub still generates its heading anchors as well and rewrites a declared id
into the same `user-content-` namespace, so both forms resolve. And a declared
id is stable across rewording, so a heading can be rewritten without breaking
inbound links from commit messages, issues, and pull requests — none of which
would ever tell you they had broken.

The shape is fixed so that abandoning the convention is cheap:

```sh
git grep -n '^<a id="' -- '*.md'
```

finds every one, and a single `sed` over those files deletes them. That sweep
also catches the worked example above, which is correct — undoing the
convention means removing the paragraph that explains it.

### `deploy/README.md` is executable, but not by CI

Its procedures carry the same `docs:run` markers and run end to end — but on
a Debian 13 host with podman 5.4.2, not on a GitHub runner. Ubuntu's `conmon`
is built without journald support, and the three long-running units set
`LogDriver=journald` deliberately, so `sre-tab-web.service` dies with
`conmon failed: exit status 1` before anything is tested. Overriding
`LogDriver` for CI would make it pass over a deployment other than the one
that ships, and a green gate on the wrong artefact is worse than no gate.

On a throwaway Debian host, as root:

```sh
export APP_BASE_URL=http://127.0.0.1:8080
export GITHUB_CLIENT_ID=not-a-real-oauth-app
export ALLOWED_GITHUB_IDS=1234567
export GITHUB_CLIENT_SECRET_FILE=/tmp/github-client-secret
umask 077 && printf 'not-a-real-secret' > "$GITHUB_CLIENT_SECRET_FILE"
python3 .github/scripts/run-doc-examples.py deploy/README.md --root .
```

`--root .` because the document's commands are written to run from the
repository root. **Throwaway** is meant literally: this installs to `/etc`,
creates podman secrets, and starts the stack.

The sequence is once-only per host, because `create-secrets.sh` refuses to run
against an existing database password. Re-running it means removing the
secrets, the volumes, *and* the containers first — removing only the secrets
leaves a database whose password nobody holds, which is exactly what that
guard exists to prevent.

The commands are extracted from `README.md` rather than copied into the
workflow, because a copy drifts and then the workflow protects nothing. A
fenced block is opted in with an HTML comment on the line before it, which no
Markdown renderer displays:

````markdown
<!-- docs:run -->
```sh
uv sync
```

<!-- docs:run background ready=http://localhost:8000/api/v1/healthz -->
```sh
uv run uvicorn app.main:app --reload
```
````

Ordinary blocks must exit zero. A `background` block is started as a job and
the runner waits for its `ready=` URL to answer before moving on — the
harness equivalent of "open a second terminal" — and everything still running
is killed when the script ends. Blocks run in document order, each from the
repository root, so a `cd` in one does not leak into the next.

To run it yourself:

```sh
python3 .github/scripts/run-doc-examples.py README.md --print   # just show the script
python3 .github/scripts/run-doc-examples.py README.md           # actually run it
```

It runs the commands **for real** in the directory holding the document,
`cp .env.example .env` included. Point it at a throwaway clone rather than at
a working tree you care about:

```sh
git clone . /tmp/sre-tab-docs && python3 /tmp/sre-tab-docs/.github/scripts/run-doc-examples.py /tmp/sre-tab-docs/README.md
```

Two consequences worth internalising. A change that breaks the quickstart —
a renamed console script, a migration that will not apply to an empty
database, a dependency that no longer resolves — fails the docs job, not just
the test suite. And a rewrite of the README that drops the markers fails
too: the runner exits non-zero when it finds no blocks, rather than passing
having checked nothing.

What it does *not* cover: `deploy/README.md`. Those procedures need a Podman
host, root, and live systemd, so they are verified by `smoke.sh` and by a
manual pass on a real Linux host. Treat prose in that file as load-bearing
and unverified unless it says otherwise — several sections say exactly which
parts have been run and which have not.

<a id="branch-protection"></a>
## Branch protection

`main` is protected. Nine checks are required. They are listed here by the
name GitHub reports — the job's `name:`, which is what a required check
actually matches on, not the job key in the workflow:

| Required check | Workflow / job | What it is |
| --- | --- | --- |
| `Format, lint, types, security, tests` | `ci.yml` / `python` | format, lint, mypy, Bandit, the header and env-example parity scripts, pytest |
| `PostgreSQL integration` | `ci.yml` / `postgres` | the PostgreSQL-only suite against a service container |
| `Dependency audit` | `ci.yml` / `audit` | `pip-audit` over the locked dependency set, and `npm audit` over the client's |
| `Static analysis` | `ci.yml` / `sast` | Semgrep, guarded against a run that scans nothing |
| `Frontend lint, types, contract, tests, build` | `ci.yml` / `frontend` | eslint, tsc, the generated API types against the committed document, the Vitest suite, vite build |
| `Container build and deployment smoke` | `ci.yml` / `container` | image build, Caddyfile validation, Quadlet generation, the deployment smoke test |
| `README quickstart runs on a clean checkout` | `docs.yml` / `quickstart` | the README's own commands, executed |
| `Relative links resolve` | `docs.yml` / `links` | every relative link and image in the docs |
| `Mermaid diagrams parse` | `docs.yml` / `diagrams` | every ```` ```mermaid ```` block in the docs, against a pinned Mermaid parser |

`Mermaid diagrams parse` was the most recent addition, and getting it there
took the ordering in [Renaming a job](#renaming-a-job): land the job, let it
report on the pull request, *then* rewrite the required set, *then* verify
against that head. The table carried a `(not yet required)` marker in the
interval, because a GitHub setting and a file cannot be changed in one diff
and a table that claims enforcement it does not have is worse than one that
admits the gap.

`Publish, sign, and attest image` is **not** required — see below.

`audit` is a separate job on purpose, and so is `sast`: both reach the network
and report on something other than the code under change, so a CVE published
this morning or a registry hiccup should not hide the test results behind the
same red cross.

<a id="renaming-a-job"></a>
### Renaming a job can silently break a required check

GitHub keys a required
status check on the check-run **context**, which for Actions is the job's
`name:` — its display name, not its key. So renaming a job can break branch
protection without breaking anything else: the old context never reports
again, and depending on how the rule is written either every pull request
waits forever for a check that will never arrive, or the job quietly stops
being enforced. Neither shows up on a push to `main`, which is where this
would be noticed if it were noticeable at all.

This is not hypothetical here: **the rule was broken from the day it was
created, and has now been fixed.**

It was configured with the job *keys* — `python`, `postgres`, `audit`,
`frontend`, `container`. Every check-run this repository reports is a display
name: the table above is the current list, and no key appears in it. The two
sets did not intersect, so every required context named a check that had
never reported and never could.

It failed safe — a pull request waits on a status that never arrives rather
than merging unchecked — but the real checks were not required either, and
nothing would ever have surfaced it: every commit on `main` is a direct push,
there has never been a pull request here, and required checks are not
consulted on that path.

The required set is the reported names, and it is verified by
set-differencing the required contexts against the check-runs the repository
actually reports — empty in the direction that matters — rather than by
reading the rule back and trusting it looked right. That check was run again
when `Mermaid diagrams parse` was added, and came back empty.

`Publish, sign, and attest image` is deliberately excluded. Its `if:` admits
a push to `main` and a push of a `v*` tag and nothing else, so it never runs
on a pull request. It is excluded on an asymmetry rather than on a
known deadlock, and the first pull request here (#4) measured half of that
asymmetry away:

- **Measured.** On a pull request the job *does* report. It appears in the
  check rollup as `Publish, sign, and attest image` with state `SKIPPED`. So
  the bad case — a required context that never reports and leaves every pull
  request pending forever, which is exactly what the broken rule did with the
  job keys — is ruled out for this job.
- **Still not measured.** Whether branch protection accepts that `skipped`
  conclusion as *satisfying* a required context. It cannot be measured while
  the job is not in the required set, and adding it to find out risks
  deadlocking the only merge path.

That is enough to keep it excluded: the exclusion costs nothing, and the
remaining unknown can only be resolved by taking the risk it exists to avoid.

**If you rename a job, update branch protection in the same change.** Nothing
in this repository can detect that you didn't: protection lives in GitHub's
settings, not in a file anyone reviews. Read the current state first, and keep
the output — the write below replaces the context list wholesale, so that read
is the only record of what was there before:

```sh
gh api repos/Darkflib/sre-tab/branches/main/protection \
  --jq '.required_status_checks.contexts'
```

Then write the full set — the current one, for reference, and the shape to
follow after a rename:

```sh
gh api -X PATCH repos/Darkflib/sre-tab/branches/main/protection/required_status_checks \
  -F strict=true \
  -f 'contexts[]=Format, lint, types, security, tests' \
  -f 'contexts[]=PostgreSQL integration' \
  -f 'contexts[]=Dependency audit' \
  -f 'contexts[]=Static analysis' \
  -f 'contexts[]=Frontend lint, types, contract, tests, build' \
  -f 'contexts[]=Container build and deployment smoke' \
  -f 'contexts[]=README quickstart runs on a clean checkout' \
  -f 'contexts[]=Relative links resolve' \
  -f 'contexts[]=Mermaid diagrams parse'
```

Then confirm it by comparing the two sets, rather than by rereading the rule:

```sh
sha=$(git rev-parse origin/main)
comm -23 \
  <(gh api repos/Darkflib/sre-tab/branches/main/protection/required_status_checks \
      --jq '.contexts[]' | sort) \
  <(gh api "repos/Darkflib/sre-tab/commits/$sha/check-runs" \
      --jq '.check_runs[].name' | sort -u)
```

Empty output means every required context is a check that actually reports.
Anything listed is a context waiting on a check that will never arrive — which
is the failure this section exists to describe, and the only way to be sure it
is absent is to look for it.

**While a rename is still in flight, compare against the pull request's head
instead.** `main` has not reported the new context yet — that is the whole
point of the change being unmerged — so the command above lists it and reads
exactly like the failure it is meant to detect. Substituting the head commit
distinguishes the two:

```sh
sha=$(gh pr view <number> --json headRefOid --jq .headRefOid)
```

This is also the ordering to follow, because it never leaves protection
naming a context that has not reported anywhere: rename the job, push, let
the new context report on the pull request, *then* rewrite the required set,
*then* verify against that head. The intermediate state is a pull request
waiting on a context that will never arrive, which is safe — it blocks a
merge rather than allowing an unchecked one — and it clears the moment the
required set is rewritten. Doing it the other way round, rewriting the rule
first, leaves the required set naming a check nothing has ever reported,
which is indistinguishable from the broken rule until CI next runs.

## Commits and pull requests

- **Conventional Commits**, with a scope where one helps:
  `fix(ingest): refuse content-codings so the size cap counts wire bytes`.
  Renovate is configured for semantic commits, so bot branches match.
- **Logical units.** One commit that does one thing, with a message saying
  why rather than restating the diff. The existing log is the reference.
- **Update [CHANGELOG.md](CHANGELOG.md)** under `## [Unreleased]`, in the
  Keep a Changelog section that fits. A security fix goes under `Security`
  with enough detail that a reader can tell whether it affected them.
- **UK English, Oxford comma**, in prose, comments, and commit messages.
- **Comments are sparse and load-bearing.** Explain the reason, not the
  mechanism; the mechanism is on the next line. Several files in this
  repository carry long comments about why something is *not* done the
  obvious way — those are the ones worth imitating.
- Do not add dependencies casually. The set is small and pinned, and every
  addition is a supply-chain decision; say why in the pull request.

## AGENTS.md is not an artefact

[AGENTS.md](AGENTS.md) holds the standing rules that let five agents build
v1 in parallel without corrupting each other's work: an ownership table, the
data-access disciplines, and the transaction rule. The parallel build is
over, but the rules were not scaffolding for it — they are the project's
actual invariants, and they still hold for human contributors:

- **2.0-style SQLAlchemy only.** `select()` / `session.execute()` /
  `session.scalars()`; no `session.query()` anywhere. Relationships declare
  `lazy="raise"`, so a request path that relies on implicit lazy loading
  fails loudly in tests instead of silently working sync-only.
- **Sessions are injected, never opened in a route.** `app.db.session.get_db`
  is the dependency; the service layer receives a `Session` and never opens
  one. This is what keeps a future `AsyncSession` migration a mechanical
  change rather than a call-graph rewrite.
- **Whoever opens the session owns the transaction.** A function that
  *receives* a `Session` never commits or rolls back — it may `flush()` to
  read its own write back. A function that *opens* one commits it. The OAuth
  callback is the reason: `upsert_user`, `ensure_profile`, `revoke_session`,
  and `create_session` are one transaction or they are a half-created
  account.
- **Contract surfaces are used, not rebuilt.** `app/api/v1/schemas/` is the
  frozen request/response contract, `app.security.tokens` and
  `app.security.csrf` are the session and CSRF primitives, `app.health.probes`
  is how a component registers a readiness check, and every configurable
  value goes through `app.settings`. A gap in any of them is a change to that
  file, discussed on its own, rather than a workaround beside it.
- **One Alembic head.** A second concurrent revision forks the graph.
  Rebase onto `main` and regenerate rather than merging two heads.

Keep it accurate. If a rule in `AGENTS.md` no longer describes how the code
works, that is a bug in the same sense a wrong README is — fix the file in
the commit that changes the behaviour.

One consequence of how this repository was built, worth knowing before you go
looking: every commit carries the same author and committer, so git records
*what* was decided and never *who* decided it. Attribute the reasoning in
these documents to the evidence cited in them, not to whoever appears to have
written the commit — during the v1 build two contributors were each credited
with the other's supposed work on the same section, and the metadata could
not settle it. Where a claim matters, it should carry the measurement that
supports it. Where it does not carry one, treat it as unsupported.

## Reporting a security issue

Open a private security advisory on the repository rather than a public
issue, and give it a few days before disclosing. The threat model that
matters most here is the feed fetcher: it is the one component that makes
outbound requests to addresses derived from stored configuration, and its
guard is where acceptance criterion 5 lives.
