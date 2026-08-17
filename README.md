# Developer News Dashboard

A self-hosted web application that lets signed-in developers aggregate
technology news, choose the topics and sources they care about, and keep
track of saved and read items. See [prd-v1.md](prd-v1.md) for the full
product requirements and [PLAN-v1.md](PLAN-v1.md) for how the build is
decomposed.

The backend is Python 3.12+ / FastAPI / SQLAlchemy 2.x (sync) / Alembic,
managed with `uv`. SQLite is used for local development and tests;
PostgreSQL is the production database.

## Status

Phase 0 (foundation) is complete: the full API contract is stubbed (all
endpoints return `501 Not Implemented`), the complete schema exists with a
single Alembic revision, and `/api/v1/openapi.json` is served and complete.
Phase 1 agents implement behaviour behind this contract — read
[AGENTS.md](AGENTS.md) before touching anything.

## Running the development server

```sh
uv sync                          # create .venv and install everything
cp .env.example .env             # then edit as needed; defaults suit dev
uv run alembic upgrade head      # create the SQLite dev database
uv run uvicorn app.main:app --reload
```

The API is served under `http://localhost:8000/api/v1`; the OpenAPI schema
is at `/api/v1/openapi.json` and interactive docs at `/docs` (disable with
`DOCS_ENABLED=false`).

## Quality gate

All of these must pass before a commit:

```sh
uv run ruff format --check .
uv run ruff check .
uv run mypy .
uv run pytest
uv run bandit -c pyproject.toml -r app
```

`uv run pre-commit install` wires the fast checks into git.

## Layout

| Path | Purpose |
| --- | --- |
| `app/` | FastAPI application package |
| `app/db/` | Engine, session factory, ORM models |
| `app/api/v1/` | Versioned API: routers and frozen Pydantic schemas |
| `app/security/` | Session-token hashing and CSRF primitives |
| `app/health.py` | Liveness/readiness probe registry |
| `alembic/` | Migration environment and revisions |
| `tests/` | Pytest suite; root `conftest.py` holds shared fixtures |
