# Linux deployment

A single Podman host with system (rootful) Quadlets, mirroring the layout
already used on `orbit-data`. Every container runs as an unprivileged numeric
user with a read-only root filesystem and all capabilities dropped; rootful
Podman is the outer context, not the inner one.

Requires **Podman 5.0 or newer** (for `Notify=healthy`) and cgroup v2.

```bash
podman --version
podman info --format '{{.Host.CgroupsVersion}}'
```

## Topology

```
                 ┌──────────────────── host ────────────────────┐
  client ─TLS─▶  │  existing reverse proxy                      │
                 │        │ 127.0.0.1:8080                      │
                 │        ▼                                     │
                 │  sre-tab-web   (Caddy, uid 65532)            │
                 │     │      │                                 │
                 │     │      └─ /srv/www ◀── sre-tab-assets    │
                 │     │  /api/*, /docs         (oneshot)       │
                 │     ▼                                        │
                 │  sre-tab-app   (uvicorn, uid 10001)          │
                 │     │                                        │
                 │     ▼                                        │
                 │  sre-tab-db    (PostgreSQL, uid 999)         │
                 │     ▲                                        │
                 │     ├── sre-tab-migrate  (oneshot, uid 10001)│
                 │     └── sre-tab-backup   (oneshot, uid 999)  │
                 └──────────────────────────────────────────────┘
```

Everything sits on `sre-tab.network` (`10.89.61.0/24`). Only Caddy publishes a
port, and only to `127.0.0.1`. The database is unreachable from the host.

### Why a proxy split rather than one process

The PRD allows either a single origin serving both, or a reverse proxy routing
`/api/` to FastAPI. This deployment takes the second option because the first
is not available: `app/main.py` mounts no static files and is frozen Phase 0
property. Caddy therefore serves `frontend/dist` and proxies `/api/*` and
`/docs` upstream. Users still see one origin.

### Why the assets live in the application image

The built frontend ships inside the application image at `/opt/sre-tab/web`,
and `sre-tab-assets.service` copies it into a volume Caddy reads. The
alternative — a second image with Caddy and the assets baked in — would mean
two artefacts moving on the same floating tag, and therefore a window where a
new API is serving an old bundle. One image, one digest, no skew.

## Host preparation

```bash
sudo deploy/install.sh
```

Idempotent. It installs the Quadlets to `/etc/containers/systemd`, the backup
timer to `/etc/systemd/system`, and the Caddyfile and backup script to
`/etc/sre-tab`. It creates `/srv/sre-tab/backups` owned by `999:999`, mode
`0750` — dumps contain every user record, so they are not world-readable.

It seeds `/etc/sre-tab/app.env` from `deploy/app.env.example` **once** and
never overwrites it afterwards. Everything else it installs is replaced on
every run: keep intentional changes in the repository, not in `/etc`.

Then edit the configuration:

```bash
sudoedit /etc/sre-tab/app.env
```

`APP_BASE_URL`, `GITHUB_REDIRECT_URI`, `GITHUB_CLIENT_ID`, and
`ALLOWED_GITHUB_IDS` all have to be set. **An empty `ALLOWED_GITHUB_IDS`
denies everyone**: v1 sign-in is allow-list only.

## Secrets

Four values are genuinely secret and none of them appears in a unit file, in
`podman inspect`, or on a command line:

| Podman secret | Consumed as | By |
| --- | --- | --- |
| `sre-tab-postgres-password` | `POSTGRES_PASSWORD`, `PGPASSWORD` | database, backup |
| `sre-tab-database-url` | `DATABASE_URL` | app, migrations |
| `sre-tab-session-secret` | `SESSION_SECRET` | app |
| `sre-tab-github-client-secret` | `GITHUB_CLIENT_SECRET` | app |

The database password appears inside `sre-tab-database-url` as well as in
`sre-tab-postgres-password`, and a mismatch between the two is a tedious way
to lose an afternoon. `create-secrets.sh` generates it once and writes both,
so they cannot disagree:

```bash
sudo deploy/scripts/create-secrets.sh < /path/to/github-client-secret
```

The GitHub client secret is read from standard input, never from an argument
or an environment variable — argv is visible to every process on the host.
Delete the file afterwards.

`SESSION_SECRET` matters more than it looks: without it the application
generates a random value per process, and every restart invalidates every
outstanding CSRF token.

### Rotating the database password

`create-secrets.sh --rotate-db` writes a new password to both secrets, but
PostgreSQL will not adopt it on its own — `POSTGRES_PASSWORD` only applies at
`initdb`. Change it in the database first, then rotate the secrets, then
restart:

```bash
podman exec -it sre-tab-db psql -U sretab -c "\password sretab"
sudo deploy/scripts/create-secrets.sh --rotate-db < /path/to/github-client-secret
sudo systemctl restart sre-tab.service sre-tab-migrate.service
```

## First start

```bash
sudo deploy/install.sh --start
```

`--start` refuses to proceed if any of the four secrets is missing. It enables
the backup timer and restarts all five units in a single `systemctl`
transaction, which is what makes systemd resolve the ordering between them
rather than starting them in the order typed.

Verify:

```bash
systemctl status sre-tab-db.service sre-tab.service sre-tab-web.service
curl --fail http://127.0.0.1:8080/api/v1/healthz
curl --fail --silent http://127.0.0.1:8080/ | head -5
```

Quadlet services are transient generated units and cannot be enabled with
`systemctl enable`; their `[Install]` sections are applied by the generator at
boot and on `daemon-reload`, so starting them explicitly is enough. The backup
timer is a native unit and is enabled normally.

## Migrations on deploy

`alembic upgrade head` runs in `sre-tab-migrate.service`, a `Type=oneshot`
unit ordered between the database and the application:

```
sre-tab-db.service  ──▶  sre-tab-migrate.service  ──▶  sre-tab.service
   Notify=healthy           Type=oneshot                Requires= both
   (pg_isready)             RemainAfterExit=yes
```

Three properties follow from that, and all three are the point of the design:

1. **The database is genuinely accepting connections first.** The database
   unit's health check is `pg_isready`, and `Notify=healthy` means systemd
   does not consider the unit started until it passes. Ordering against a
   container that merely *exists* would race `initdb` on a first deployment.
2. **Migrations are not a race between replicas.** One unit runs them, once,
   per host transaction. The application unit `Requires=` it, so a failed
   migration stops the application from starting against a schema it does not
   match — rather than starting it and failing at the first query.
3. **A plain application restart does not re-run them.** `RemainAfterExit=yes`
   keeps the unit active after it exits, so `systemctl restart sre-tab.service`
   starts only the application. An upgrade restarts the migration unit
   explicitly (below).

The migration command retries ten times at three-second intervals. That is not
a substitute for the ordering above; it covers the narrower case of the
database restarting underneath an already correctly ordered migration.

## Upgrading

The units track `ghcr.io/darkflib/sre-tab:latest` with `Pull=newer`, so a
restart adopts the current build:

```bash
sudo systemctl restart \
  sre-tab-migrate.service \
  sre-tab-assets.service \
  sre-tab.service \
  sre-tab-web.service
```

One `systemctl` invocation, not four: systemd builds a single transaction and
honours the `After=` ordering inside it.

For deliberate, reviewable upgrades, pin `Image=` in
`deploy/quadlet/sre-tab.container`, `sre-tab-migrate.container`, and
`sre-tab-assets.container` to a `:sha-<commit>` tag instead of `:latest`, and
change the pin as a commit. Take a backup before any upgrade that carries a
migration; `alembic downgrade` is not a substitute for a restore.

A deploy causes a sub-second blip while Caddy restarts and the assets volume
is replaced. That is expected for a single-instance self-hosted service. If it
ever needs to be zero-downtime, the pattern to reach for is orbit-data's
atomic release symlink rather than a destructive copy.

## TLS termination and what the outer proxy must not do

Caddy listens on `127.0.0.1:8080` only. Terminate TLS at the host's existing
proxy or load balancer and forward to that address.

The application sets its own security headers in `app/middleware.py`, and
Caddy sets a verbatim mirror of them on the files it serves from disk — the
SPA document above all, which never reaches the middleware and is where CSP is
actually enforced. `deploy/scripts/check-header-parity.sh` compares the two and
CI fails on drift.

The outer proxy must therefore:

- **Not strip or rewrite** `Content-Security-Policy`, `X-Content-Type-Options`,
  `Referrer-Policy`, `X-Frame-Options`, `Cross-Origin-Opener-Policy`,
  `Cross-Origin-Resource-Policy`, or `Permissions-Policy`. Several proxies
  helpfully add their own CSP; two `Content-Security-Policy` headers are
  intersected by the browser, and the result is usually a broken page.
- **Not add its own `Cache-Control`.** `index.html` is deliberately
  `no-store` and `/assets/*` deliberately `immutable`; overriding either is
  how a deployment lands and nobody sees it.
- **Set `X-Forwarded-For` itself, and refuse a client-supplied one.** Caddy is
  the inner of two proxies. If the outer one does not populate it, every
  request reaches the application looking as though it came from the proxy,
  and per-IP rate limiting silently becomes global rate limiting.
- **Serve the origin in `APP_BASE_URL` over HTTPS.** The application builds
  absolute URLs from that setting rather than from forwarded headers, so no
  `X-Forwarded-Proto` plumbing is needed — but the value has to be right.

### The client-address chain, and why it needs settings at both ends

Setting `X-Forwarded-For` at the outer proxy is necessary and, on its own,
not sufficient. There are three links, and all three have to hold or the
application's per-IP rate limiting quietly becomes one global bucket — with
nothing in the logs to say so, because every link is behaving exactly as
documented.

```
client ─▶ TLS proxy ─▶ Caddy ─────────────▶ uvicorn ─▶ app
          sets XFF     appends its peer     picks one   rate-limit key
                       (trusted_proxies)    (FORWARDED_ALLOW_IPS)
```

1. **The outer proxy sets `X-Forwarded-For`** to the real client, and refuses
   one the client supplied.
2. **Caddy appends rather than replaces.** Caddy 2.7 and later will not
   believe an `X-Forwarded-For` from a peer it does not trust — sensibly —
   and *replaces* it with the connecting address instead. So without the
   `servers { trusted_proxies … }` block in `deploy/Caddyfile`, the outer
   proxy's header is thrown away at the front door and step 1 buys nothing.
   The default trusts `10.89.61.1/32`, the gateway on `sre-tab.network`,
   which is the source address a connection to the published
   `127.0.0.1:8080` port arrives from once Podman has DNATed it.
3. **uvicorn resolves the chain.** It reads `X-Forwarded-For` only when the
   peer is in `FORWARDED_ALLOW_IPS`, then walks the chain from the right and
   takes the first address it does *not* trust. So that list has to name
   every hop we operate — Caddy at `10.89.61.20` for the peer check, and
   `10.89.61.1` for the hop Caddy appended — or the walk stops one short and
   the "client" is a proxy.

Add a hop and both step 2 and step 3 need it. A CDN in front of the TLS proxy
means its address goes in `trusted_proxies` and in `FORWARDED_ALLOW_IPS`.

Never set `FORWARDED_ALLOW_IPS=*`. That branch makes uvicorn take the
*leftmost* value in the chain, which is whatever the client wrote, and a
caller can then pick its own rate-limit bucket.

Check it end to end after any change to either — the application does not log
the client address on the happy path, so the honest test is to spend the
budget and see whether it is shared:

```bash
# From two different client addresses. Twenty starts in five minutes is the
# limit, per address. If the second client is throttled by the first client's
# traffic, the chain is broken somewhere above.
for i in $(seq 1 21); do
  curl -so /dev/null -w '%{http_code}\n' https://news.example.com/api/v1/auth/github/start
done | tail -3
```

A 429 on the twenty-first request from one address, and a 302 from a
different address at the same moment, is the property working.

### Two settings that need a frontend rebuild, not a restart

The frontend reads the CSRF cookie and header names at build time, defaulting
to the same values as `.env.example`. Change either of these server-side and
the bundle has to be rebuilt with the matching value, or every mutating
request fails CSRF:

| Server setting | Frontend build variable | Default |
| --- | --- | --- |
| `CSRF_COOKIE_NAME` | `VITE_CSRF_COOKIE_NAME` | `csrftoken` |
| `CSRF_HEADER_NAME` | `VITE_CSRF_HEADER_NAME` | `X-CSRF-Token` |

Leave both alone unless a proxy forces the issue. Nothing else in the
frontend is configured at build time; all API paths are relative.

### Egress

The feed fetcher is the only component that talks to the internet, and it does
so from `10.89.61.0/24`. If that traffic needs to leave by a specific source
address, the nftables SNAT approach in `orbit-data/deploy/README.md` applies
unchanged — substitute this subnet and add the fragment to the host firewall.

## Backups

`sre-tab-backup.timer` runs daily at 03:22 UTC with up to 20 minutes of
jitter, and `Persistent=true` so a host that was off overnight takes its backup
on the way back up rather than waiting another day.

Each run writes `pg_dump --format=custom --compress=zstd` to
`/srv/sre-tab/backups`, verifies it with `pg_restore --list` before publishing
it, renames it into place atomically, writes a `.sha256` sidecar, and prunes
dumps older than `BACKUP_KEEP_DAYS` (14). Verifying at write time is the
difference between a backup job that fails visibly and a backup that only
reveals itself as useless during a restore.

```bash
systemctl list-timers 'sre-tab-*'
systemctl start sre-tab-backup.service     # take one now
journalctl -u sre-tab-backup.service --since today
ls -l /srv/sre-tab/backups
```

**`/srv/sre-tab/backups` is on the same host as the database.** That is a
backup, not disaster recovery. Copy the directory off-host on whatever
schedule the operator's risk tolerance justifies; the `.sha256` sidecars exist
so a copy can be verified at the far end.

## Restore

```bash
sudo deploy/scripts/restore.sh /srv/sre-tab/backups/sretab-20260817T032200Z.dump
```

This destroys the target database and rebuilds it from the dump. The script:

1. verifies the `.sha256` sidecar, then verifies the dump parses with
   `pg_restore --list`, **before** anything is dropped;
2. prompts for the database name as confirmation (`--yes` skips it);
3. stops `sre-tab.service` and `sre-tab-migrate.service` so nothing writes
   during the restore;
4. `DROP DATABASE ... WITH (FORCE)` and recreates it;
5. restores with `--single-transaction --exit-on-error`, so the database ends
   up either fully restored or empty for a second attempt — never
   half-restored with the application then migrating on top of it;
6. reports the table count and the `alembic_version` row;
7. restarts the application and waits for `/api/v1/healthz`.

Point-in-time recovery is out of scope for v1: these are nightly logical
dumps, so the recovery point is the last successful backup.

### The restore has been tested

`deploy/scripts/smoke.sh` runs the same `restore.sh`, unmodified, against a
throwaway database: it brings up PostgreSQL on an empty volume, migrates,
starts the application and Caddy, checks the health endpoint and the front-door
behaviour, writes a marker row, takes a backup with the real `backup.sh`, drops
the marker, restores with the real `restore.sh`, and asserts that the marker
and the Alembic revision both come back and the application goes healthy
again.

```bash
CONTAINER_ENGINE=docker SRE_TAB_IMAGE=sre-tab:dev deploy/scripts/smoke.sh
```

CI runs it on every push under Podman. It has been run under Docker on macOS
during development and passes end to end.

What that does **not** cover, and what still needs one pass on a real Linux
host before release: Quadlet generation into live systemd units, the `After=`
and `Requires=` ordering actually holding at boot, `Notify=healthy`, the
Podman secret plumbing, and the timer firing. CI validates unit *generation*
with `podman-system-generator --dryrun`, which catches malformed keys but not
runtime behaviour.

## Seeding the catalogue, and the operator CLI

The database starts with no sources and no topics, so a freshly migrated
instance shows an empty feed and an onboarding screen with nothing to tick.
Seed it once, after the first `install.sh --start`:

```bash
podman exec sre-tab-app sre-tab seed
```

Idempotent, and it never overwrites an existing row — an operator who renamed
a source, changed its interval, or disabled it has made a decision, and
re-running the seed does not reverse it.

The rest of the administrator role from the PRD lives in the same command:

```bash
podman exec sre-tab-app sre-tab sources list
podman exec sre-tab-app sre-tab topics list
podman exec sre-tab-app sre-tab sources disable bbc-news
podman exec sre-tab-app sre-tab sources set-topics lobsters --topics open-source,devops
podman exec sre-tab-app sre-tab sources add \
  --slug phoronix --name Phoronix \
  --feed-url https://www.phoronix.com/rss.php \
  --website-url https://www.phoronix.com/ \
  --topics hardware --refresh-minutes 60
podman exec sre-tab-app sre-tab sources add-medium-tag python --topics python
```

Three things about it are worth knowing before using it.

**A feed URL is validated when it is added, not when it is first fetched.**
`sre-tab sources add` runs the whole SSRF guard minus DNS — https only, no
credentials, port 443, no private or obfuscated IP literals, no single-label
or non-public hostname — and refuses GraphQL and sitemap endpoints, which are
the v2 deferral. A URL the fetcher would reject is rejected here, with the
reason, rather than becoming a source that silently never works.

**`add-medium-tag` expands the template at configuration time.** The tag has
to match a strict slug pattern, and what lands in `sources.feed_url` is a
fixed string. Nothing in the fetch path ever assembles a URL from a value it
did not already have; acceptance criterion 5 depends on that.

**Sources with no topics produce items with no topics.** Items inherit their
source's default topics at ingest, and `GET /feed?topics=…` is literal — an
item carrying no topics matches no explicit topic filter. Always pass
`--topics` when adding a source.

### Refresh status

```bash
podman exec sre-tab-app sre-tab status
```

This is the PRD's operator status view. It reads the `source_status` table
rather than the application's memory, which is what lets a separate process
answer the question at all — and it exits non-zero when an enabled source is
failing, so a monitoring job can call it and mean it:

```bash
podman exec sre-tab-app sre-tab status || echo 'a source is failing'
```

`source_status` is deliberately not columns on `sources`: `sources` is
operator-managed configuration and this is scheduler-written runtime state.
Keeping them apart means `sources.updated_at` still means "the operator
changed the configuration", and the two writers never contend.

## Operations

```bash
systemctl list-timers 'sre-tab-*'
systemctl status sre-tab-db.service sre-tab.service sre-tab-web.service
journalctl -u sre-tab.service --since today
journalctl -u sre-tab-migrate.service -n 50
podman logs sre-tab-web
curl --fail http://127.0.0.1:8080/api/v1/healthz | jq
```

`/api/v1/healthz` distinguishes liveness from readiness and names each probe,
so a 503 says which dependency is unhappy rather than merely that something
is.

The oneshot units set `LogDriver=none`: systemd already captures the
container's stdout into the journal under its own unit, and Podman's journald
driver would write a second copy of every structured line. The long-running
containers keep `LogDriver=journald` deliberately, because `podman logs` is
worth having for them.

There is no `OnFailure=` alert unit, unlike orbit-data — that project's alert
path is a subcommand of its own application, and this one has no equivalent
yet. Until it does, failures surface through `systemctl --failed` and the
journal. Wiring an alert to the operator CLI is a reasonable Phase 2 follow-up.

## Database major-version upgrades

`postgres:18` moved `PGDATA` to `/var/lib/postgresql/18/docker` and declares
its volume one level up, so `sre-tab-db.container` mounts
`/var/lib/postgresql` — which would have been wrong on `postgres:17`. Read
that line before changing the pin, and take a backup first. Renovate holds
major bumps on the dependency dashboard rather than opening a PR, for exactly
this reason.
