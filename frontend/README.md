# Frontend — Developer News Dashboard

React client for the v1 API. Built with Vite, generated against the frozen
`/api/v1/openapi.json` contract, served same-origin with the API.

## Commands

| Command | What it does |
| --- | --- |
| `npm ci` | Install exactly the locked dependency set |
| `npm run dev` | Dev server on `http://localhost:5173`, proxying `/api` to `http://localhost:8000` |
| `npm run build` | Production build into `frontend/dist` |
| `npm run preview` | Serve the built output locally |
| `npm run lint` | ESLint 9, flat config, type-aware rules |
| `npm run typecheck` | `tsc --build --force` over app and tooling projects |
| `npm test` | Vitest, one shot |
| `npm run test:watch` | Vitest, watching |
| `npm run check` | lint + typecheck + test + build, in that order |
| `npm run generate:api` | Regenerate `src/api/schema.d.ts` from `openapi.json` |

Node 20.19 or newer (Vite 8's floor); CI and the container build should pin
Node 24.

## Tests

Vitest, in its default `node` environment. That is a deliberate constraint
rather than an oversight: the theme tests install by hand every global the
theme layer touches, so a new dependency on, say, `window.sessionStorage`
shows up as a failure instead of being supplied silently by an ambient DOM.
`theme-init.test.ts` executes `public/theme-init.js` in a `node:vm` context,
and `tokens.test.ts` parses `tokens.css` and recomputes WCAG contrast ratios.
None of that wants a browser.

Four files do. `src/api/client.test.ts`,
`src/data/usePagedResource.effects.test.ts`,
`src/routes/ApiTokensSection.test.tsx`, and
`src/components/FilterBar.collapse.test.tsx` each opt in with a docblock:

```ts
// @vitest-environment happy-dom
```

Per-file, not in `vite.config.ts`, so the rest of the suite keeps its
property of failing loudly when it reaches for a global it did not install.
`happy-dom` is the only test-environment dependency; there is no
request-mocking library (`globalThis.fetch` is stubbed with `vi.fn()`) and no
renderer library (`usePagedResource.effects.test.ts` mounts the hook on
`createRoot` with React 19's own `act`, in about thirty lines, and
`FilterBar.collapse.test.tsx` mounts a component the same way).

`client.ts` genuinely needs the DOM rather than merely liking it: the client
is built with no `baseUrl`, so openapi-fetch constructs
`new Request('/api/v1/me')` with a root-relative URL. A browser resolves that
against the document; Node's undici throws.

## Deployment shape

- **Build command:** `npm ci && npm run build`
- **Output directory:** `frontend/dist`
- **Base path:** `/`. The app is served at the origin root; there is no
  sub-path build.
- **History fallback:** required. Client routes are `/`, `/onboarding`,
  `/feed`, `/bookmarks`, `/settings`; unknown paths must fall back to
  `index.html`, *except* `/api/*`, which proxies upstream to FastAPI.
- **Caching:** `dist/assets/*` are content-hashed and safe to serve
  `immutable`. `dist/index.html` and `dist/theme-init.js` are not hashed and
  must be revalidated.
- **Build-time environment:** none required. Three optional overrides exist
  and should stay unset unless the operator has changed the matching server
  setting:

  | Variable | Default | Server counterpart |
  | --- | --- | --- |
  | `VITE_CSRF_COOKIE_NAME` | `csrftoken` | `CSRF_COOKIE_NAME` |
  | `VITE_CSRF_HEADER_NAME` | `X-CSRF-Token` | `CSRF_HEADER_NAME` |

  Changing either server-side requires a frontend rebuild.

## Content Security Policy

The server sends `script-src 'self'; style-src 'self'` with no
`'unsafe-inline'`, so the build contains **no inline script or style**:

- `public/theme-init.js` is a separate same-origin file, loaded blocking in
  `<head>`. It resolves the stored theme before first paint, which is what
  stops a light/dark flash. It must not be inlined.
- Icons are inline SVG elements in JSX, not `data:` URIs, and
  `assetsInlineLimit` is `0` so nothing else becomes one either.
- React writes the `style` prop through the CSSOM rather than the `style`
  attribute, so dynamic values (the composition bars) are unaffected by
  `style-src`.
- `img-src 'self' https: data:` is load-bearing: feed images, source icons,
  and GitHub avatars are third-party hosts.

If a reverse proxy serves `dist/` directly rather than FastAPI serving it,
the proxy must set the CSP and the other security headers itself — the
application middleware only decorates responses FastAPI emits.

## The API client

`src/api/schema.d.ts` is generated, not written:

```sh
LOG_LEVEL=CRITICAL uv run python -c "import json; from app.main import create_app; print(json.dumps(create_app().openapi(), indent=2))" > frontend/openapi.json
cd frontend && npm run generate:api
```

`LOG_LEVEL=CRITICAL` is not optional tidiness: structlog writes to stdout, so
`create_app()`'s start-up lines land in the middle of the JSON otherwise.

`openapi.json` is committed so the build never depends on a running server
or a Python toolchain. When the contract changes, regenerate both files in
one commit; a contract change then shows up as a type error rather than a
runtime surprise.

That used to be a sentence asking for discipline. It is a gate now, split
across the two jobs that already have the toolchain for each link:

- `tests/test_openapi.py` compares `openapi.json` against the schema the
  application serves, byte for byte, and names these two commands when it
  fails;
- the `frontend` CI job regenerates `schema.d.ts` and fails on a diff.

Forgetting the second command is the interesting case, and the one neither
`tsc` nor the test suite could ever have caught on its own: the types stay
internally consistent with a document that has stopped describing the
server, so the typecheck goes on passing — faithfully, against the wrong
contract.

`src/api/client.ts` is the only module that touches the network. It adds:

- `credentials: 'same-origin'`, so the `HttpOnly` session cookie rides along
  and no token is ever held in JavaScript;
- the CSRF header on every `POST`/`PUT`/`PATCH`/`DELETE`, read from the
  double-submit cookie;
- a 401 hook — any 401 anywhere flips the session to signed-out, which
  returns the user to the landing page from wherever they were.

## Layout

```
src/
  api/          generated schema, fetch client, endpoint wrappers
  catalogue/    GET /sources, shared by feed filters, onboarding, settings
  components/   shell, filter bar, item card, icons, shared states
  data/         cursor-pagination hook
  feed/         filter model, collapse state, volume analysis, feed hooks
  lib/          formatting, CSS, and localStorage helpers
  routes/       landing, onboarding, feed, bookmarks, settings, auth guard
  session/      GET /me, preference updates, sign-out, account deletion
  styles/       tokens, base, app
  theme/        theme resolution and application
```

## Filtering and the volume asymmetry

The catalogue mixes sources whose publication rates differ by an order of
magnitude. A feed ordered purely by publication time belongs to the fastest
of them within the hour, so filtering is the primary control on the feed
screen rather than a secondary one:

- Topic and source chips sit in the page flow above the feed, not behind a
  menu, and the active state is stated in words as well as colour
  ("Your selection" versus "Filtered").
- Filters live in the URL query string, so they survive reload, back, and
  sharing, and never change the saved profile. "Save as my default" is the
  explicit way to promote a filter set into `PATCH /me/preferences`.
- Sources refreshing every 15 minutes or faster are flagged **high volume**
  from `refresh_minutes` — a signal available before a single item loads —
  and are left switched off in onboarding.
- A composition panel measures each source's share of what has actually
  loaded, with one-click *Only* and *Hide*.
- When one source exceeds 35% of the loaded items, a notice says so and
  offers to hide or isolate it.
- The chips collapse. That argument is about where the control lives, not
  about how much of the viewport it keeps once a reader has used it — on a
  phone it is most of the first screen, and after a selection is settled it
  is noise on any screen. The bar is a disclosure with `aria-expanded` and
  `aria-controls`, the state is kept per device in `localStorage` (see
  `src/feed/collapse.ts`), and expanded is the default, so a first visit is
  unchanged.
- **A collapsed bar still says what it is filtering.** The badge, the
  counts, and "Clear filters" never collapse, and a summary line names the
  sources and topics the counts cannot — with `null` and `[]` kept apart, so
  an un-overridden dimension contributes nothing and a deselect-everything
  says "No sources selected" outright.

## Preferences

`max_visible_cards` doubles as the feed page size: it is the number of items
the user asked to have in front of them at once. `layout` switches the item
list between grid and single-column. `theme` honours light, dark, and
system; system follows `prefers-color-scheme` live, and the last choice is
mirrored into `localStorage` purely as a first-paint hint — the server
profile remains authoritative.
