# Implementation Plan — Developer News Dashboard v1

Companion to [prd-v1.md](prd-v1.md). Describes how v1 is decomposed across
sub-agents, what each owns, and the sequencing constraints between them.

## Decomposition principle

The naive split of a greenfield service — "one agent per feature" on day zero —
fails here for a specific reason: every feature in this PRD imports the same
substrate. Models, settings, the session dependency, the Pydantic response
schemas, and the Alembic revision graph are shared by all of it. Fan out before
those exist and you get five agents each inventing an incompatible `User` model
and a forked migration history.

So: **one agent builds the contract, then everything else runs in parallel
against it.** The contract is deliberately over-specified — Phase 0 produces
the complete ORM layer, the complete Pydantic request/response schemas, and
stub routes that return `501` with correct signatures. That makes
`/api/v1/openapi.json` real on day one, which is what lets the frontend and the
API implementation proceed genuinely concurrently rather than in a chain.

## Shared-contract conflicts and how they are avoided

These are the files that would otherwise be edited by several agents at once.
Each is resolved by making it Phase 0's exclusive property and freezing it.

| Conflict | Resolution |
| --- | --- |
| ORM models | Phase 0 writes all of `app/db/models.py` from the PRD data model. No Phase 1 agent edits it; schema gaps are raised, not patched. |
| Alembic revisions | Phase 0 generates the single initial migration for the whole schema. Phase 1 agents write **no** migrations — a second concurrent revision forks the graph. |
| `pyproject.toml` | Phase 0 pins the full dependency set up front. **No agent runs `uv add`.** A missing dependency is escalated, not installed. |
| API router aggregator | Phase 0 writes `app/api/v1/router.py` with every `include_router` already wired to the stub modules. Nobody touches it again; agents fill in their own module. |
| `/healthz` readiness | Phase 0 exposes a probe *registry*; the ingest agent registers a scheduler probe rather than editing the health endpoint. |
| `tests/conftest.py` | Phase 0 owns root fixtures (app, db, client, authed client). Agents add fixtures only under their own `tests/<area>/conftest.py`. |

## Phase 0 — Foundation (serial, single agent)

No useful parallelism. One agent, start to finish, and nothing else runs until
it lands.

- `git init`, repo baseline (README, AGENTS.md, WORKLOG.md, CHANGELOG.md).
- `uv` + `pyproject.toml` with the complete dependency set; Ruff, mypy, Bandit,
  pytest, pre-commit configured and passing on an empty tree.
- `app/settings.py` (pydantic-settings, all PRD env vars), `app/logging.py`
  (structlog, request-ID middleware, secret/PII redaction filter).
- SQLAlchemy 2.x declarative base, session factory, **all models**, Alembic
  scaffold + initial revision. Verified `upgrade`/`downgrade` on SQLite and
  Postgres.
- FastAPI app factory, `/api/v1` mount, security-headers middleware (CSP,
  `X-Content-Type-Options`, `Referrer-Policy`, frame protections), CSRF
  primitive, health probe registry.
- `get_current_user` dependency **signature only**, raising `NotImplementedError`
  — so Phase 1 agents can import and depend on it before auth exists.
- **Async-portability discipline**, encoded in AGENTS.md and the models: 2.0
  `select()` style only, no implicit lazy loading in request paths
  (`raiseload` default so slips fail loudly), sessions confined to the service
  layer via DI. Sync `Session` is the v1 choice; these three habits are what
  keep a later `AsyncSession` migration a contained, mechanical change instead
  of a call-graph rewrite.
- Complete Pydantic v2 schemas + stub routes returning `501` for all 12 PRD
  endpoints. `openapi.json` is complete and correct at the end of this phase.

**Done when:** `alembic upgrade head` works on both engines, `openapi.json`
matches the PRD endpoint table, and the lint/type/test gate is green.

## Phase 1 — Fan-out (5 agents, parallel, disjoint paths)

Each agent owns its directories exclusively. Overlap is the failure mode; the
table above is what prevents it.

### A — Auth and sessions
**Owns:** `app/auth/`, `app/api/v1/auth.py`, `app/api/v1/me.py`, `tests/auth/`

GitHub OAuth authorization-code flow server-side; `state` generation and
validation; exact allow-listed redirect URI; session issuance with **hashed**
token at rest; `HttpOnly`/`Secure`/`SameSite=Lax` cookie; CSRF enforcement on
mutating routes; logout; `DELETE /me` cascade across preferences, bookmarks,
reads, sessions; rate limiting on OAuth start and callback failures; the real
`get_current_user`. Client secret and access token never reach browser code or
logs.

Authorisation in v1 is a static allow-list of GitHub numeric user IDs read from
an env var, checked at callback before any user record is created. No GitHub
org-membership API calls. The check sits behind a single seam so v2 can swap in
org or team resolution without touching the flow around it.

### B — Ingest and scheduling
**Owns:** `app/ingest/`, `app/scheduler/`, `tests/ingest/`

Highest-risk component; carries acceptance criterion 5. SSRF guard first and
tested in isolation: allow only configured source URLs, enforce HTTPS, resolve
DNS and block private/link-local/reserved ranges, re-check on every redirect
hop, short timeouts, response-size cap, fetch rate limiting — **all before any
socket is opened**. Then RSS/Atom parsing, field normalisation, summary
sanitisation to text, canonical-URL dedup, per-source failure isolation,
APScheduler with a Postgres advisory-lock leader strategy, the 90-day prune
job, and a structured per-source status surface.

Parsing is RSS and Atom only. A source that needs sitemap crawling, GraphQL, or
any other bespoke adapter is rejected at configuration time and deferred to v2
— the fetcher does not grow special cases.

No HTTP surface of its own beyond registering the scheduler readiness probe.
Fully independent of A and C.

### C — Feed, preferences, and user state
**Owns:** `app/api/v1/{feed,sources,items,bookmarks}.py`, `app/services/`,
`tests/api/`

Replaces the `501` stubs: `GET /feed` with cursor pagination (default 25, max
100) and topic/source filters, `GET /sources`, `PATCH /me/preferences`,
read-state, bookmarks. Idempotent writes leaning on the compound unique
constraints; every mutation in a transaction; strict per-user scoping so one
user's state is unreachable from another's session. Depends on A only through
the `get_current_user` signature Phase 0 provided, so it does not wait on A.

### D — Frontend
**Owns:** `frontend/`

React client generated against `openapi.json`. Landing/sign-in, onboarding
topic and source selection, feed with filters, item open marking read in a new
tab, bookmarks view, settings (theme, layout, card count). Same-origin, cookie
auth, CSRF header on mutations. Blocked only by Phase 0.

### E — Build, deploy, CI
**Owns:** `Containerfile`, `deploy/`, `.github/workflows/`, `.env.example`

Unprivileged rootless container; Podman quadlets; `.env.example` covering every
setting; GitHub Actions running format, lint, type check, tests, `pip-audit`,
Bandit, and a container build; documented Postgres backup and a **tested**
restore. Independent of all application code.

Quadlet layout mirrors `orbit-data`; this agent reads
`/Users/mike/dev/orbit-data/deploy/quadlet/` first and follows its conventions
— including the dedicated egress network seen there — rather than inventing a
second house style.

## Phase 2 — Integration (serial, single agent)

Swap the real `get_current_user` in behind C's routes, delete residual stubs,
end-to-end migration run against a fresh Postgres, migration test against both
empty and populated databases, seed the initial source catalogue, and the
operator CLI for source and topic management plus the refresh-status view.

## Phase 3 — Verification (parallel, adversarial)

Run against the integrated tree, not against individual agents' work.

- `/security-review` over the auth and ingest paths specifically.
- `sast` skill — Semgrep, Bandit, Trufflehog, pip-audit.
- Existing wwff-tech auditors map directly onto agent E's output:
  `github-actions-auditor` on the workflows, `dockerfile-auditor` on the
  Containerfile, `podman-quadlet-hardener` on `deploy/`.
- An acceptance agent that walks the seven v1 criteria and demonstrates each,
  rather than asserting them.
- p95 check on `GET /feed` at 25 items against the 400 ms target.

## Acceptance criteria ownership

| # | Criterion | Owner | Verified by |
| --- | --- | --- | --- |
| 1 | GitHub sign-in/out, no duplicate users | A | Phase 3 acceptance |
| 2 | Scheduled fetch, no duplicate items | B | Phase 3 acceptance |
| 3 | Preferences persist across browsers | C + D | Phase 3 acceptance |
| 4 | Idempotent, user-scoped bookmarks and reads | C | Phase 3 acceptance |
| 5 | Unsafe fetch targets rejected pre-network | B | `/security-review` |
| 6 | Fresh Postgres deploy, migrations, health | Phase 2 + E | Phase 3 acceptance |
| 7 | CI gate complete | E | `github-actions-auditor` |

## Agent roster

Write these as project-scoped definitions in `.claude/agents/` rather than
passing ad-hoc prompts. The briefs get reused across retries and Phase 3
re-runs, and each definition is where the owned-paths constraint is stated so
it survives context compaction.

Every brief carries the same three standing rules: do not edit files outside
your owned paths; do not run `uv add`; do not generate Alembic revisions.

## Sequencing

Phase 0 is the critical path and everything queues behind it — worth doing
carefully rather than quickly. Phase 1 is a genuine five-way fan-out with no
inter-agent blocking. Phase 2 is short. Phase 3 is parallel and read-only.

## Decisions (locked)

1. **Public sign-up disabled by default.** Access is a static allow-list of
   GitHub numeric user IDs supplied by env var.
2. **Catalogue** — see below. RSS/Atom only in v1.
3. **New frontend.** The existing Hackertab extension is not reused; it may be
   read for interaction and layout inspiration only.
4. **No org-membership checks in v1.** The static allow-list from 1 covers the
   initial handful of users; richer authorisation defers to v2. This narrows
   *authorisation*, not *authentication* — the full OAuth flow, session
   hashing, cookie flags, and CSRF remain v1 scope under acceptance criterion 1.
5. **Single Podman host with quadlets**, mirroring the layout already used on
   `orbit-data`.

### Deferred to v2

Sources that are not RSS/Atom. Hashnode needs sitemap parsing or GraphQL, and
anything else requiring a bespoke adapter follows the same rule. Agent B's
fetcher targets RSS and Atom only; a non-conforming source is a configuration
error, not a parser special case.

## Initial source catalogue

All first-party feeds, so there is no third-party aggregator in the dependency
path. Seeded by the Phase 2 operator CLI.

| Source | Feed | Default topics | Refresh |
| --- | --- | --- | --- |
| Hacker News | `news.ycombinator.com/rss` | tech-industry | 15 min |
| Lobsters | `lobste.rs/rss` | open-source, tech-industry | 30 min |
| Dev.to | `dev.to/feed` | webdev | 30 min |
| LWN | `lwn.net/headlines/newrss` | open-source, security | 60 min |
| Ars Technica | `feeds.arstechnica.com/arstechnica/index/` | tech-industry, science | 30 min |
| BBC News | `feeds.bbci.co.uk/news/rss.xml` | uk-news, world-news | 15 min |
| Guardian UK | `theguardian.com/uk/rss` | uk-news | 30 min |
| Medium (per tag) | `medium.com/feed/tag/<tag>` | per tag | 60 min |

**Medium is a template, not a source.** Each tag becomes its own ordinary
`sources` row with its own slug — `medium-webdev`, `medium-python` — and the
operator CLI gets an `add-medium-tag <tag>` helper that expands the template at
configuration time. This keeps every fetchable URL an explicitly configured
one, which is what acceptance criterion 5 depends on; a runtime-templated URL
would put an untrusted path component back into the fetch path.

**Topic taxonomy widens.** The catalogue is no longer purely developer news, so
the seed taxonomy needs general-news topics alongside the technical ones:
`webdev`, `python`, `devops`, `security`, `open-source`, `ai-ml`, `hardware`,
`tech-industry`, `science`, `uk-news`, `world-news`.

**Volume asymmetry is now a real UX constraint.** BBC, Guardian, and Ars
publish at far higher rates than Lobsters or LWN. A feed ordered purely by
publication time will be dominated by them within an hour. Topic and source
filtering is therefore load-bearing in v1 rather than a convenience, and the
onboarding defaults should not enable every source at once. Flagged to agents
C and D; no schema change implied.
