# Roadmap

Work deliberately deferred past v1. Items are grouped by why they were
deferred, not by size. See [prd-v1.md](prd-v1.md) for v1 scope and
[PLAN-v1.md](PLAN-v1.md) for how it was built.

## Supply-chain hygiene

Raised by the Phase 3 SAST pass. The dependency trees are clean — 90 Python
packages and 331 npm packages, zero CVEs, zero verified secrets across full
git history — so this section is about the pipeline, not the code.

- **Digest-pin the application image.** Every third-party image is pinned by
  tag *and* digest; ours floats on `:latest` with `Pull=newer` across
  `sre-tab.container`, `sre-tab-migrate.container`, and
  `sre-tab-assets.container`. It is the single unpinned link in an otherwise
  fully pinned chain, and CI pushes `:latest` on every merge to main, so a
  restart silently adopts whatever that currently resolves to. The units
  already document a `:sha-<commit>` escape hatch; the work is making that
  the default and giving deploys an explicit promotion step.
- **Sign and verify.** No cosign signing and no SLSA provenance today, so
  nothing is checked at admission. This compounds the floating tag: an
  unpinned reference *and* an unverified one.
- **Generate an SBOM** per build and retain it with the image.
- **Extend Renovate to the CI workflows.** It currently tracks `deploy/`
  only, so GitHub Actions versions drift unwatched.
- **Fail CI on a non-empty Semgrep `errors[]`.** The `p/bash` ruleset 404s,
  and that failure aborted a whole scan while still emitting a well-formed
  report with `results: 0` and `paths.scanned: []` — a green gate that had
  scanned nothing. Semgrep's OSS rules are thinner than Pro but worth
  keeping; the risk is a silently broken run, not a shallow one.
- **Supplement JS/TS coverage.** A canary with `innerHTML = param`,
  `eval(param)`, and a literal AWS key went unflagged by the anonymous
  ruleset. ShellCheck already covers the shell scripts (7 files, clean).

## API surface

- **`docs_enabled` should default to `False`.** It defaults `True` today, so
  a deployment not derived from `deploy/app.env.example` exposes Swagger UI.
  Default-closed, opt in for development.
- **Serve a static OpenAPI document in production** rather than generating it
  from the live app. Publishing the schema at `/api/v1/openapi.json` is a v1
  requirement and stays; the change is decoupling it from the running
  application so the served artefact is a reviewed, versioned file.

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
  at the time. It has one now.
- **Frontend unit tests.** The build, lint, and typecheck gates pass and the
  client was driven manually in a browser, but there are no unit tests. The
  known gap.

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
