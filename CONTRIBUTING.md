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
is the watcher. CI runs all four as separate steps, so `npm run check` is the
local equivalent of the `frontend` job.

Three further checks run in CI and are worth knowing about before a change
that touches their subject matter:

| Command | Fails when |
| --- | --- |
| `deploy/scripts/check-env-example.sh` | a setting exists in `app/settings.py` and not in `.env.example` |
| `deploy/scripts/check-header-parity.sh` | the security headers in `app/middleware.py` and `deploy/Caddyfile` disagree |
| `SRE_TAB_POSTGRES_URL=... uv run pytest tests/postgres` | a PostgreSQL-only path breaks |

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
docker build --tag sre-tab:dev .
CONTAINER_ENGINE=docker SRE_TAB_IMAGE=sre-tab:dev deploy/scripts/smoke.sh
```

CI does the same under Podman, where the build needs `--format docker`: OCI
format has no `HEALTHCHECK` field, and `sre-tab.container`'s `Notify=healthy`
waits on the image's healthcheck, so an OCI-format build hangs the unit until
`TimeoutStartSec` instead of failing fast.

Run it on a host that is not already running the stack. It uses the real
container names, deliberately, so that the Caddyfile's upstream is tested
verbatim rather than against a renamed copy.

## Documentation is executed, not proofread

`.github/workflows/docs.yml` runs the README's quickstart on a clean checkout
on every push. This is not decoration. Two documented procedures in this
repository have been wrong while reading perfectly: `install.sh --start`
never recreated a removed network, and the upgrade procedure was wrong as
written. Both were found by running them on Linux, not by reviewing them.

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

## Branch protection

`main` is protected. Five checks are required, all from
`.github/workflows/ci.yml`:

| Check | What it is |
| --- | --- |
| `python` | format, lint, mypy, Bandit, the header and env-example parity scripts, pytest |
| `postgres` | the PostgreSQL-only suite against a service container |
| `audit` | `pip-audit` over the locked dependency set, and `npm audit` over the client's |
| `frontend` | eslint, tsc, the Vitest suite, vite build |
| `container` | image build, Caddyfile validation, Quadlet generation, the deployment smoke test |

`audit` is a separate job on purpose, and so is `sast`: both reach the network
and report on something other than the code under change, so a CVE published
this morning or a registry hiccup should not hide the test results behind the
same red cross.

### Renaming a job can silently break a required check

The names in that table are the job *keys* in `ci.yml`. GitHub keys a required
status check on the check-run **context**, which for Actions is the job's
`name:` — its display name, not its key. So renaming a job can break branch
protection without breaking anything else: the old context never reports
again, and depending on how the rule is written either every pull request
waits forever for a check that will never arrive, or the job quietly stops
being enforced. Neither shows up on a push to `main`, which is where this
would be noticed if it were noticeable at all.

This is not hypothetical here, and the problem is worse than a rename.

**The rule was configured with job keys** — `python`, `postgres`, `audit`,
`frontend`, `container` — the identifiers on the left of each job in
`ci.yml`. GitHub does not match on those. It matches on the check-run name,
which is the job's `name:` whenever one is set, and every job here sets one.
The names GitHub actually reports are `Format, lint, types, security, tests`,
`PostgreSQL integration`, `Dependency audit`, `Static analysis`,
`Frontend lint, types, tests, build`, and
`Container build and deployment smoke`.

So every required context names something that has never reported and never
will. The failure is at least in the safe direction — a pull request waits on
a status that never arrives rather than merging unchecked — but the required
set is enforcing nothing, and the real checks are not required. It went
unnoticed because every commit so far has gone straight to `main` by an
administrator, and required checks are not consulted on that path.

The `frontend` rename is therefore a second problem behind the first: it
matters once the contexts are corrected, not before.

Fixing it needs admin scope. Read the current state:

```sh
gh api repos/Darkflib/sre-tab/branches/main/protection \
  --jq '.required_status_checks.contexts'
```

Job keys in that list mean the rule is broken as described. Display names
mean it is working, provided each one still matches a job's current `name:`.

Then set the contexts to the check-run names, omitting
`Publish, sign, and attest image` — that job only runs on a push to `main`,
so requiring it would leave every pull request permanently unsatisfiable,
which is the same trap in the opposite direction:

```sh
gh api -X PATCH repos/Darkflib/sre-tab/branches/main/protection/required_status_checks \
  -F strict=true \
  -f 'contexts[]=Format, lint, types, security, tests' \
  -f 'contexts[]=PostgreSQL integration' \
  -f 'contexts[]=Dependency audit' \
  -f 'contexts[]=Static analysis' \
  -f 'contexts[]=Frontend lint, types, tests, build' \
  -f 'contexts[]=Container build and deployment smoke' \
  -f 'contexts[]=README quickstart runs on a clean checkout' \
  -f 'contexts[]=Relative links resolve'
```

Neither command could be run on the day: the endpoint answered `503`
throughout GitHub's incident of 17 August 2026. That was diagnosed rather
than assumed — an ordinary repository read succeeded with the same token
while two *different* admin-scope endpoints both returned 503, and a
permissions failure answers 403 or 404, not 503 on some endpoints and 200 on
others. Until the PATCH above has been applied and confirmed, treat the
required set as **not enforcing anything**, rather than as probably enforced.

The general rule this leaves behind: **a job rename is a branch-protection
change**, and the two have to move together.

Two more jobs run and are **not** in the required set, because both are new
and a required check should have a run history before it can block a merge:
`sast` in `ci.yml`, and the `quickstart` and `links` jobs in `docs.yml`. Both
should join it. A pull request that makes either flaky is a bug in that job,
not a reason to ignore it.

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

## Reporting a security issue

Open a private security advisory on the repository rather than a public
issue, and give it a few days before disclosing. The threat model that
matters most here is the feed fetcher: it is the one component that makes
outbound requests to addresses derived from stored configuration, and its
guard is where acceptance criterion 5 lives.
