# Agent rules

Binding rules for every agent working in this repository.

Most of this file was written for v1's parallel build, where several agents
worked at once in one tree. That is over, and the rules that were *purely*
about coordination are marked below as such. The rest are live invariants —
they describe how the code is put together, not how it was built, and
breaking one now breaks the same thing it would have broken then.

If a rule here blocks you, say so rather than working around it.

## Standing rules

- **Ownership** (coordination-era): do not edit files outside your owned
  paths — see the table below. Working alone, this collapses to the
  ordinary courtesy of not making unrelated changes in the same commit.
- **Never run `uv add` or edit `pyproject.toml` casually.** A dependency
  is a supply-chain decision now, not a convenience: the lockfile is
  audited by `pip-audit` in CI, and everything in it ships in the image.
- **Never generate an Alembic revision without meaning to.** The
  migration history is verified in both directions against SQLite *and*
  PostgreSQL, including against a populated database. A second concurrent
  revision forks the graph, which is why this was absolute during the
  parallel build and remains a considered act now.
- **`app/db/models.py`, `app/api/v1/router.py`, root `tests/conftest.py`,
  and the health endpoint are shared surfaces.** Readiness checks are
  registered through the probe registry rather than by editing the
  endpoint; extra fixtures live in `tests/<area>/conftest.py`. This is
  still the right shape — the registry is what lets the scheduler report
  readiness without the health module knowing the scheduler exists.

## Two rules that outlived the parallel build

- **Attribute reasoning to the evidence cited, not to the author.** Every
  commit in this repository carries the same author and committer, so git
  records what was decided and never who decided it. A claim with no
  measurement behind it is unsupported, whoever appears to have written
  it. This is not hypothetical: two agents each credited the other with
  the same work, and the metadata could not settle it.
- **A green check is not a passed check.** Six times in this project a
  gate existed, reported success, and verified nothing: a CI assertion
  querying the wrong JSON key, a Semgrep run that scanned zero files, a
  CSRF check bypassed by a test fixture, a test suite that ran nowhere, a
  size cap counting bytes after the allocation it existed to prevent, and
  a branch-protection rule naming checks that never report. When you add
  a guard, make it fail once on purpose before you believe it.

## Data-access rules

The service is sync SQLAlchemy by contract, but a later async migration
must stay cheap. Three disciplines are therefore part of the contract:

1. All queries in 2.0 style — `select()` / `session.execute()` /
   `session.scalars()`. No legacy `session.query()` anywhere.
2. No reliance on implicit lazy loading in request paths — relationships
   used by the API are loaded explicitly (`selectinload` or joins). The
   models declare `lazy="raise"` on every relationship, so a lazy-load
   slip fails loudly in tests instead of silently working sync-only.
3. Session access is confined to the service layer and injected as a
   dependency (`app.db.session.get_db`) — routes never open sessions
   themselves. This contains the function-colour change if `Session` ever
   becomes `AsyncSession`.

## Transactions

**Whoever opens the session owns the transaction.** One rule, no
exceptions:

- A function that *receives* a `Session` never calls `commit()` or
  `rollback()`. It may `flush()` when it needs to read its own write
  back. That is every service in `app/services/`, every helper in
  `app/ingest/store.py`, and everything in `app/auth/` below the route.
- A function that *opens* a session commits it. In a request that is the
  route, whose session comes from `get_db`; in the scheduler it is
  `IngestService`, which opens its own.
- `get_db` deliberately does not commit. It closes the session, and
  closing rolls back anything uncommitted, so a route that raises before
  its commit leaves nothing behind.

The point is composability: a self-committing service cannot be called
twice in one unit of work, and the OAuth callback already needs exactly
that — `upsert_user`, `ensure_profile`, `revoke_session`, and
`create_session` are one transaction or they are a half-created account.

## Ownership

Each Phase 1 agent owns its paths exclusively. Overlap is the failure
mode; this table is what prevents it.

| Agent | Owns |
| --- | --- |
| A — Auth and sessions | `app/auth/`, `app/api/v1/auth.py`, `app/api/v1/me.py`, `tests/auth/` |
| B — Ingest and scheduling | `app/ingest/`, `app/scheduler/`, `tests/ingest/` |
| C — Feed, preferences, and user state | `app/api/v1/{feed,sources,items,bookmarks}.py`, `app/services/`, `tests/api/` |
| D — Frontend | `frontend/` |
| E — Build, deploy, CI | `Containerfile`, `deploy/`, `.github/workflows/`, `.env.example` |

Phase 2 added `app/cli/` (operator CLI and the seed catalogue),
`tests/cli/`, and `tests/postgres/`, and lifted the freeze on the
shared-contract files below for integration work only. Phase 3 is
read-only against the integrated tree.

Shared-contract files owned by Phase 0 and frozen for Phase 1:

| File | Resolution |
| --- | --- |
| ORM models | Phase 0 wrote all of `app/db/models.py` from the PRD data model. No Phase 1 agent edits it; schema gaps are raised, not patched. |
| Alembic revisions | Phase 0 generated the single initial migration for the whole schema. Phase 1 agents write **no** migrations — a second concurrent revision forks the graph. |
| `pyproject.toml` | Phase 0 pinned the full dependency set up front. **No agent runs `uv add`.** A missing dependency is escalated, not installed. |
| API router aggregator | Phase 0 wrote `app/api/v1/router.py` with every `include_router` already wired to the stub modules. Nobody touches it again; agents fill in their own module. |
| `/healthz` readiness | Phase 0 exposes a probe *registry*; the ingest agent registers a scheduler probe rather than editing the health endpoint. |
| `tests/conftest.py` | Phase 0 owns root fixtures (app, db, client, authed client). Agents add fixtures only under their own `tests/<area>/conftest.py`. |

## Contract surfaces Phase 1 must use, not rebuild

- **Schemas** — `app/api/v1/schemas/` is the frozen request/response
  contract. Import from it; a gap or mistake in a schema is escalated,
  not patched in place.
- **`get_current_user`** — import from `app.api.deps`. It currently
  raises `501`; Agent A replaces its body without changing the
  signature. Everyone else depends on it as-is. Tests override it via
  the `authed_client` fixture.
- **Sessions at rest** — use `app.security.tokens`
  (`generate_session_token` / `hash_session_token`). Only the hash is
  stored; the raw token exists solely in the cookie.
- **CSRF** — `app.security.csrf` implements the signed double-submit
  cookie primitive. The token is bound to the session it was issued for
  (`generate_csrf_token(secret, session_token)`), so one minted for
  another session is refused. Mutating routes take
  `Depends(require_csrf)`; Agent A issues the cookie at session creation.
- **Probes** — `from app.health import probes;
  probes.register_readiness("scheduler", fn)`. Never touch the healthz
  route.
- **Settings** — every configurable value goes through `app.settings`.
  New env vars are an escalation (settings.py is Phase 0 property), with
  one exception: Agent E owns `.env.example` and keeps it in sync with
  settings.py.
- **Logging** — `structlog.get_logger()`; request IDs are bound
  automatically. Never log OAuth codes, access tokens, cookie values, or
  full preference payloads — the redaction filter is a backstop, not a
  licence.
- **Rate limiting** (Agent A) — implement in-process with stdlib
  primitives; the v1 deployment is single-instance and no rate-limit
  dependency is provided.

## Quality gate

Run before every commit; all must pass:

```sh
uv run ruff format --check .
uv run ruff check .
uv run mypy .
uv run pytest
uv run bandit -c pyproject.toml -r app
```

UK English in prose and docs. Comments sparse and load-bearing. Commit in
logical units with clear messages.
