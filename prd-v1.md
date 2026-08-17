# Product Requirements Document — Developer News Dashboard v1

## Summary

Build a self-hosted web application that lets signed-in developers aggregate
technology news, select the topics and sources they care about, and keep track
of saved and read items. The product replaces the dependency on Hackertab's
unpublished API with an independently operated service.

The first release prioritizes a dependable personal/small-team dashboard over
feature parity with the existing Hackertab site. It deliberately excludes ads,
payments, analytics, browser-extension distribution, and recommendation/AI
features.

## Problem

Developer news is spread across many sites. Users need one private place to
see a curated, current set of items without relying on a third-party hosted
service or surrendering their preferences and reading history to it.

## Goals

1. Let a user sign in safely with their GitHub account.
2. Show a useful, deduplicated feed from administrator-configured sources.
3. Allow users to control which topics and sources appear in their feed.
4. Persist user preferences, bookmarks, and read state on the server.
5. Be straightforward to self-host on one machine initially and migrate to
   PostgreSQL for production.

## Non-goals for v1

- Browser extensions or new-tab replacement.
- Per-device preference overrides (planned for v2).
- Google or email/password sign-in.
- Ads, subscriptions, payments, affiliate links, or telemetry by default.
- AI ranking, personalised recommendations, social features, or notifications.
- Arbitrary user-supplied RSS URLs. This avoids making v1 an SSRF-capable RSS
  proxy; sources are administrator-managed.
- Exact API or visual compatibility with the old Hackertab application.

## Users and roles

### Member

An authenticated user who can view the feed and manage their own preferences,
bookmarks, and read state.

### Administrator

An instance operator who configures feed sources and their topic mappings. In
v1 this can be an operator-only CLI or an admin database flag; an admin UI is
not required.

## Core user journeys

### First use and sign-in

1. A visitor opens the site and sees a short explanation plus **Sign in with
   GitHub**.
2. The visitor completes GitHub OAuth and returns to the application.
3. The application creates or updates the local user record and issues a
   secure session.
4. The user chooses initial topics and enabled sources, then lands on a feed.

### Consume the feed

1. The user opens the home page.
2. The application shows recent eligible feed items ordered by publication
   time, with duplicate canonical URLs removed.
3. The user can filter by enabled topic and source.
4. Opening an item marks it read; the destination opens in a new tab.
5. The user may bookmark an item and later view or remove bookmarks.

### Manage preferences

1. The user opens Settings.
2. They enable/disable topics and sources, choose light/dark/system theme,
   choose their preferred layout, and set the number of visible cards.
3. Changes are saved server-side and appear consistently from another browser.

## Functional requirements

### Authentication and account lifecycle

- GitHub is the only v1 identity provider, using OAuth 2.0 authorization-code
  flow on the server.
- The server must validate `state`, use an exact allow-listed redirect URI, and
  never expose the GitHub client secret or OAuth access token to browser code.
- A local user is identified by GitHub's stable numeric user ID, not login name
  or email. Store the display name, avatar URL, and login as mutable profile
  data.
- Authentication is a secure, `HttpOnly`, `Secure`, `SameSite=Lax` cookie.
  The web UI and API are same-origin in v1. State-changing requests require
  CSRF protection.
- Users can sign out and request deletion of their account and associated
  preferences, bookmarks, reads, and sessions. Retain only minimal operational
  audit records if the operator enables them.

### Feed and sources

- The operator configures each source with name, RSS/Atom URL, website URL,
  enabled state, refresh interval, default topic mappings, and optional icon.
- A scheduled worker fetches enabled sources, parses RSS/Atom, normalises
  fields, validates URLs, deduplicates items, and stores them locally.
- The feed exposes title, canonical URL, source, topics, publication time,
  optional summary, and optional image URL.
- Item canonical URLs are unique across the instance. Fetch failures must not
  remove previously stored items.
- The API supports cursor pagination, source filtering, topic filtering, and
  a bounded page size (default 25, maximum 100).
- The initial source set should be small and reliable: Hacker News, Lobsters,
  Dev.to, and a limited set of administrator-approved engineering blogs.

### Preferences and reading state

- Each user has one server-side v1 preference profile.
- Profile fields: selected topics, enabled source IDs, theme, layout, maximum
  visible cards, and onboarding completion state.
- Sensible defaults are created at first sign-in. A user with no selection sees
  the instance defaults.
- Marking an item read is idempotent. Users can mark an item unread.
- Bookmarks are unique per user and item, and support listing and removal.

### Application API

All routes are versioned under `/api/v1` and return JSON.

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz` | Liveness and basic dependency status |
| `GET /auth/github/start` | Initiate GitHub OAuth |
| `GET /auth/github/callback` | Complete OAuth and create a session |
| `POST /auth/logout` | End the current session |
| `GET /me` | Current user and their preference profile |
| `PATCH /me/preferences` | Update the current profile |
| `DELETE /me` | Delete current user data and sign out |
| `GET /sources` | List enabled sources and topic metadata |
| `GET /feed` | Paginated feed with `topics`, `sources`, and `cursor` filters |
| `PUT /items/{item_id}/read-state` | Mark read or unread |
| `GET /bookmarks` | List current user's bookmarks |
| `PUT /items/{item_id}/bookmark` | Create a bookmark idempotently |
| `DELETE /items/{item_id}/bookmark` | Remove a bookmark |

The API schema must be generated from FastAPI and published at
`/api/v1/openapi.json`; production interactive docs may be operator-restricted.

## Data model

Use SQLAlchemy 2.x with Alembic migrations. SQLite is supported for local
development; PostgreSQL is the production database.

| Entity | Essential fields |
| --- | --- |
| `users` | `id`, `github_id` (unique), `github_login`, `display_name`, `avatar_url`, `is_admin`, timestamps |
| `sessions` | `id`, `user_id`, `expires_at`, `revoked_at`, timestamps |
| `user_preferences` | `user_id` (unique), `theme`, `layout`, `max_visible_cards`, `onboarding_completed`, timestamps |
| `user_preference_topics` | `user_id`, `topic_id` |
| `user_preference_sources` | `user_id`, `source_id` |
| `topics` | `id`, `slug` (unique), `name`, `enabled` |
| `sources` | `id`, `slug` (unique), `name`, `feed_url`, `website_url`, `refresh_minutes`, `enabled`, timestamps |
| `source_topics` | `source_id`, `topic_id` |
| `feed_items` | `id`, `source_id`, `canonical_url` (unique), `title`, `summary`, `published_at`, `image_url`, `fetched_at` |
| `feed_item_topics` | `feed_item_id`, `topic_id` |
| `user_read_items` | `user_id`, `feed_item_id`, `read_at` |
| `bookmarks` | `user_id`, `feed_item_id`, `created_at` |

Use compound unique constraints for join/state tables to make repeated client
requests safe. Store OAuth secrets only in deployment secrets, never in these
tables. If persistent sessions are used, store only a hashed session token.

## Technical and operational requirements

### Backend

- Python 3.12+ service using FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic,
  PostgreSQL in production, and SQLite for local development.
- Use `uv` with `pyproject.toml`; enforce Ruff formatting/linting, mypy,
  pylint where useful, Bandit, pytest, and pre-commit.
- Use structured logs with request IDs. Do not log OAuth codes, access tokens,
  cookies, or full user preferences.
- A scheduler (APScheduler in the application process for v1) refreshes feeds.
  It must use a single-leader/lock strategy in PostgreSQL so multiple web
  replicas cannot fetch the same source concurrently. Move scheduling to a
  separate worker before scaling horizontally.
- No Celery. RabbitMQ is out of scope unless asynchronous workloads require it
  after v1.

### Deployment

- The application must run as an unprivileged container/process behind a TLS
  reverse proxy. Prefer Podman/Kubernetes-compatible manifests.
- Persist PostgreSQL data and back it up daily. Test restore before release.
- Configuration is via environment variables with a documented `.env.example`:
  `DATABASE_URL`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`,
  `GITHUB_REDIRECT_URI`, `SESSION_SECRET`, `APP_BASE_URL`, and source-refresh
  settings.
- The same origin serves the built frontend and API, or a reverse proxy routes
  `/api/` to FastAPI. Set a restrictive CSP, `X-Content-Type-Options`,
  `Referrer-Policy`, and frame protections.

### Security and reliability

- RSS fetching must allow only configured URLs, enforce HTTPS where possible,
  resolve and block private/link-local/reserved IP ranges, re-check redirects,
  use short timeouts, cap response size, and rate-limit fetches.
- Validate and sanitize stored summaries; render text, not arbitrary feed HTML.
- Rate-limit OAuth initiation/callback failures and authenticated mutating API
  operations.
- Use database transactions for preference, read-state, and bookmark updates.
- Health checks distinguish liveness from database and scheduler readiness.
- Retain feed items for a configurable window (default 90 days) and prune in a
  scheduled task.

## Non-functional targets

- Home/feed API p95 below 400 ms against a warm local database for 25 items.
- Feed refresh failures are visible in structured logs and an operator status
  view/CLI; a source failure does not affect other sources.
- A single small instance supports at least 100 active users and 25 sources.
- Core API and feed-normalisation paths have automated tests; migrations are
  tested against empty and populated databases.
- No external analytics are enabled by default.

## v1 acceptance criteria

1. A new user can sign in with GitHub, sign out, and later sign in to the same
   account without creating a duplicate user.
2. A configured source is fetched on schedule; valid items become visible in
   `/api/v1/feed`, and repeated items do not duplicate.
3. Users can update topics, sources, theme, layout, and card count; a fresh
   browser session receives the saved profile.
4. A user can bookmark and mark items read/unread; these actions are
   idempotent and inaccessible to other users.
5. Invalid/unsafe RSS fetch targets are rejected before any network request.
6. The app starts with documented environment variables, runs migrations, and
   passes health checks on a fresh PostgreSQL deployment.
7. CI runs format, lint, type checks, tests, dependency/security checks, and a
   container build.

## v2: per-device preferences

v2 adds optional device-scoped overrides without losing the v1 account-level
profile. A browser registers a random device ID stored in a first-party cookie
or local storage; server rows keyed by `(user_id, device_id)` hold only fields
the user explicitly overrides. Reads merge device values over account values.
The v1 schema should therefore keep account preferences separate from sessions
and avoid treating a session as a device identity.

## Decisions still needed

1. Is this strictly personal/small-team, or may public sign-up be enabled?
2. Which feeds should ship in the initial source catalogue, and who can change
   them?
3. Does the first UI reuse parts of the existing React client, or is it a new
   frontend?
4. Should GitHub OAuth be limited to an organisation or allow any GitHub user?
5. What deployment target is preferred: a single Podman host, Kubernetes, or a
   managed PostgreSQL platform?
