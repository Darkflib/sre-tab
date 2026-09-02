<!--
Thanks for this. A few things this repository asks for that are not the usual
boilerplate — AGENTS.md and CONTRIBUTING.md carry the reasoning.
-->

## What changes, and why

<!-- The why is the part that survives. A diff shows what; nothing else
     records what you decided against, or what measurement settled it. -->

## How you know it works

<!-- "A green check is not a passed check." If you added a test or a guard,
     say how you watched it fail — a guard that has never gone red is not
     evidence. If you could not verify something, say so plainly here; an
     honest gap is worth more than an implied claim. -->

## Checklist

- [ ] `uv run ruff format --check . && uv run ruff check . && uv run mypy . && uv run pytest && uv run bandit -c pyproject.toml -r app`
- [ ] `cd frontend && npm run check`, if the frontend changed
- [ ] `shellcheck` on any shell script touched
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`, in the voice of the entries around it
- [ ] `ROADMAP.md` updated if this closes, opens, or changes an item there
- [ ] No new dependency, or a new dependency justified in the description — the lockfile is audited in CI and everything in it ships in the image
- [ ] No Alembic revision, or a deliberate one — the migration history is verified in both directions against SQLite and PostgreSQL
