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
`0700`.

Those ids mean `postgres` *inside the postgres image*, not on the host —
**on Debian 13 gid 999 is `systemd-journal`**, a group operators do add people
to. Hence `0700` and not `0750`, and hence `umask 077` in `backup.sh`: a dump
is a complete copy of every user record, and neither the directory mode nor
the file mode should be the only thing protecting it. Nothing legitimate needs
group access — the backup container writes as uid 999 and `restore.sh` runs as
root.

It seeds `/etc/sre-tab/app.env` from `deploy/app.env.example` **once** and
never overwrites it afterwards. Everything else it installs is replaced on
every run: keep intentional changes in the repository, not in `/etc`.

Then edit the configuration:

```bash
sudoedit /etc/sre-tab/app.env
```

`APP_BASE_URL`, `GITHUB_REDIRECT_URI`, `GITHUB_CLIENT_ID`, and
`ALLOWED_GITHUB_IDS` all have to be set.

### `ALLOWED_GITHUB_IDS` is the first-deploy trap

**An empty `ALLOWED_GITHUB_IDS` denies everyone.** v1 sign-in is allow-list
only — a comma-separated list of **numeric** GitHub user IDs, checked at the
OAuth callback before any user record is created — and empty means nobody,
including the operator who just deployed it.

This is correct fail-closed behaviour and it looks nothing like a policy
decision from the browser. The GitHub authorisation succeeds, the browser
returns to `/api/v1/auth/github/callback`, and the response is a bare `403`
carrying `{"detail": "This GitHub account is not permitted to sign in."}` —
which reads far more like a broken OAuth application than like an allow-list
doing its job. Authorisation is checked before any write, so a denied
identity also leaves no row behind to hint at what happened.

`deploy/app.env.example` ships the initial operator allow-list rather than an
empty value, so an installation seeded from it works. An installation whose
`app.env` came from anywhere else — the root `.env.example`, a configuration
management system, a hand-written file — starts closed. Check it before
concluding the OAuth app is misconfigured:

```bash
grep ALLOWED_GITHUB_IDS /etc/sre-tab/app.env
journalctl -u sre-tab.service --since '5 min ago' \
  | grep -e oauth_callback_denied -e oauth_denied_not_allow_listed
```

`oauth_denied_not_allow_listed` carries the `github_id` that was refused,
which is the fastest way to discover that the number in the list is a
repository ID, an organisation ID, or a digit short.

The value is the numeric `id` from `https://api.github.com/users/<login>`,
not the login itself. A login can be changed and reused; a numeric ID cannot,
which is why the allow-list and the `users` table both key on it.

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

An upgrade is a commit, not a restart. The three application units pin
`ghcr.io/darkflib/sre-tab:sha-<commit>@sha256:<digest>` with `Pull=missing`,
so restarting them re-runs the build that is already pinned and nothing else.
Changing which build runs happens in the repository, where it can be reviewed
and reverted.

They used to track `:latest` with `Pull=newer`, which meant `systemctl
restart` — a reboot, an OOM kill, a routine restart to clear a stuck
connection — silently adopted whatever CI had last pushed to main. The
running version was decided by whoever merged most recently.

### Promote a build

From a checkout, on any machine with `curl`, `cosign`, and `git`:

```bash
git fetch origin
deploy/scripts/promote.sh                 # the build published for origin/main
deploy/scripts/promote.sh 1a2b3c4         # or a specific commit
```

It resolves the commit to the digest the registry actually serves, verifies
that digest, and only then rewrites all three units. If verification fails it
writes nothing:

```
==> cosign verify ghcr.io/darkflib/sre-tab@sha256:…
Error: no signatures found
```

Review and commit the result — the diff is three `Image=` lines, and they
must always move together, because migrations, the application, and the
frontend assets ship in one image so they cannot skew. CI enforces both that
they agree and that the digest they name is signed.

### Apply it on the host

```bash
git pull
sudo deploy/install.sh
sudo systemctl restart \
  sre-tab-migrate.service \
  sre-tab-assets.service \
  sre-tab.service \
  sre-tab-web.service
```

One `systemctl` invocation, not four: systemd builds a single transaction and
honours the `After=` ordering inside it. The first start after a promotion
pulls the new digest; later restarts do not touch the network, because a
digest names immutable content and the local copy is by definition the right
one.

Take a backup before any upgrade that carries a migration; `alembic
downgrade` is not a substitute for a restore.

A deploy causes a sub-second blip while Caddy restarts and the assets volume
is replaced. That is expected for a single-instance self-hosted service. If it
ever needs to be zero-downtime, the pattern to reach for is orbit-data's
atomic release symlink rather than a destructive copy.

### What actually verifies what

Every published image is signed with cosign using GitHub's OIDC identity —
no key to store or rotate — and carries SLSA build provenance and an SPDX
SBOM. Verify any of it by hand:

```bash
deploy/scripts/verify-image.sh                    # the pinned digest
deploy/scripts/verify-image.sh ghcr.io/darkflib/sre-tab@sha256:…
```

Be clear about where that check runs, because the honest answer is "not at
container start":

| Point | Runs | Catches |
| --- | --- | --- |
| Publish (CI) | every push to main | a signature or attestation that cannot be verified from outside the step that made it |
| Promotion (`promote.sh`) | when a digest is chosen | pinning a build that is not ours |
| CI, every push and PR | always | a pin that was hand-edited, or that has stopped verifying |
| Operator, before a restart | when run | the above, on the host, at the moment of deploying |
| **Container start** | **never** | **nothing** |

That last row is not an oversight. Podman *can* enforce signatures at pull
time through `containers-policy.json`, but its `sigstoreSigned` requirement
expresses a keyless identity only as `fulcio.oidcIssuer` plus
`fulcio.subjectEmail`, and **both are mandatory**. A GitHub Actions
certificate has no email: its subject is a URI,
`https://github.com/Darkflib/sre-tab/.github/workflows/ci.yml@refs/heads/main`.
There is no field to put that in, so this identity cannot be written into a
podman signature policy at all. Checked on Debian 13 with podman 5.4.2 rather
than read in the man page — podman rejects the policy itself:

```
$ podman pull --signature-policy /tmp/pol.json ghcr.io/darkflib/sre-tab@sha256:…
Error: invalid policy in "/tmp/pol.json": subjectEmail not specified
```

The enforcement machinery works; it is the identity that cannot be expressed.
A policy of `{"type": "reject"}` scoped to this repository does refuse the
pull, on the same host, with `Source image rejected: … is rejected by
policy` — so podman is consulting the policy and would enforce a signature
requirement it could describe.

What the pin gives you without it is still worth having: podman will refuse
content that does not hash to the pinned digest, so the *wrong version* case
is closed by the digest and the *wrong image* case is closed at promotion,
once, by a human running a script that refuses to be talked out of it. What
remains uncovered is a host whose local image store was tampered with after
a verified pull.

If that matters more than availability does, `verify-image.sh` can be wired
into the start path as a drop-in — `sudo systemctl edit sre-tab.service`,
then `ExecStartPre=/usr/local/bin/verify-image.sh`. Understand the trade
before doing it: the application then needs cosign, reachable Fulcio and
Rekor endpoints, and a working network *to start at all*, so a registry
outage during a reboot becomes an outage of this service. That is why it is
not the default.

### Changes to `sre-tab.network` need the network removed

`sre-tab-network.service` runs `podman network create --ignore` and has no
`ExecStop`, so it is a no-op against a network that already exists. Editing
`sre-tab.network` — the subnet, the gateway, `IPRange=` — therefore changes
nothing until the network object itself is removed, and `podman network reload`
does not do it either. Containers hold the network, so they come down first:

```bash
sudo systemctl stop sre-tab-web.service sre-tab.service sre-tab-db.service
sudo podman network rm systemd-sre-tab
sudo deploy/install.sh --start
```

Quadlet names the network `systemd-` plus the unit name, hence
`systemd-sre-tab` rather than `sre-tab`. Confirm the result with
`podman network inspect systemd-sre-tab`; an upgrade that skips this leaves the
old, rangeless network in place and the address-collision fix inert.

Note the second step is `podman network rm`, not `systemctl stop
sre-tab-network.service`. The unit is a `RemainAfterExit` oneshot and stays
`active (exited)` whether or not its network still exists, which is why
`install.sh --start` restarts it explicitly — without that, removing the
network by hand leaves every container failing with `unable to find network
with name or ID systemd-sre-tab` and no unit looking guilty.

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

Two things reach the internet, both from `10.89.61.0/24` and both from the
application container. The feed fetcher is the obvious one, and the only one
whose destinations an operator configures. The other is the OAuth callback:
`app/auth/github.py` exchanges the authorisation code at
`github.com/login/oauth/access_token` and reads the profile from
`api.github.com/user`, both on a ten-second timeout with redirects not
followed. An egress policy that allows only the configured feed hosts will
therefore break sign-in.

If that traffic needs to leave by a specific source address, the nftables
SNAT approach in `orbit-data/deploy/README.md` applies unchanged — substitute
this subnet and add the fragment to the host firewall.

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

**The `Persistent=true` catch-up has not been demonstrated.** What is tested
is the script: `deploy/scripts/smoke.sh` runs the real `backup.sh` and the
real `restore.sh` on every push and asserts the dump, its `.sha256` sidecar,
and a restore that brings back both a marker row and the Alembic revision.
What is not tested is the scheduling around it. Proving catch-up needs the
host down across 03:22 UTC and then brought back, and no machine running this
stack has yet been off overnight.

The behaviour is systemd's rather than ours and this is an unremarkable
`OnCalendar=` / `Persistent=true` pair, so the risk is small — but small is
not tested, and an operator whose host is routinely off overnight should
satisfy themselves before relying on it. `systemctl list-timers 'sre-tab-*'`
shows the next and last elapse, which is where a missed catch-up would first
be visible.

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

Four things about it are worth knowing before using it.

**A slug has to be lower-case letters and digits joined by single hyphens**,
and at most 64 characters. `sources add` and `topics add` refuse anything
else. This is not cosmetic: the slug is written into the browser's query
string, joined into the client's cache key, and matched against the database
in the feed query, and those consumers do not agree about what characters
mean. A slug containing a comma is split in two on its way through the URL,
so the source lists correctly and filters to nothing, with no error anywhere.
Refusing it here is the same trade the feed-URL check makes below — fail
where the mistake was typed, not three components downstream. `sre-tab
status` reports any slug that predates the check and exits non-zero; such a
slug fetches normally and has to be re-added under a valid one, because
rewriting it in place would break every saved selection naming it.

**A feed URL is validated when it is added, not when it is first fetched.**
`sre-tab sources add` runs the whole SSRF guard minus DNS — https only, no
credentials, port 443, no private or obfuscated IP literals, no single-label
or non-public hostname — and refuses GraphQL and sitemap endpoints, which are
the v2 deferral. A URL the fetcher would reject is rejected here, with the
reason, rather than becoming a source that silently never works.

There is one failure it cannot predict, because predicting it would need the
request the check deliberately does not make: **a URL whose origin redirects to
`http://` can never be fetched.** The guard is https-only on every hop,
redirect hops included, so the fetch stops at the downgrade — correctly, and
the CLI exits non-zero with the reason — but nothing about the URL as typed
says it will happen. A trailing slash is the usual way to land on one:

```
https://www.theguardian.com/uk/rss/   → 301 http://www.theguardian.com/uk/rss   refused
https://www.theguardian.com/uk/rss    → 200                                     fine
```

The whole failure is silent at add time and loud afterwards. `sources add`
accepts the trailing-slash URL and prints `added source …`. On the first
refresh the fetch stops at the downgrade, and the source then reads:

```
SLUG            STATE        LAST FETCH            LAST SUCCESS  LAST ERROR
guardian-slash  failing (1)  2026-08-17 13:52:46Z  —             UnsafeTargetError

guardian-slash: UnsafeTargetError: refused scheme:
  http://www.theguardian.com/uk/rss (scheme 'http' is not https)
```

`sre-tab status` then exits non-zero, so a monitoring job that calls it starts
alerting on a source that was accepted an hour earlier. That is the intended
behaviour of every part of this, and it is thoroughly mystifying the first
time. If a source never fetches, request its feed URL by hand and read the
`Location` header before concluding the feed is down.

The opposite case — a redirect that stays on `https` — is followed, with the
destination re-validated and re-pinned in its own right rather than merely
trusted. Worth knowing that this branch has unit coverage but no real-world
provenance: none of the nineteen candidate feeds surveyed for the v1
catalogue redirects at all, so the first source that does will be the first
live exercise of it.

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
is. The database check is bounded at five seconds: a database that has been
*frozen* rather than stopped — a paused container, a stalled volume, a
black-holed route — answers nothing at all and has no socket error to report,
so without a deadline the probe simply never returns and a sick dependency
reads as a sick application. It now degrades promptly instead, well inside the
proxy's 30-second header timeout.

The oneshot units set `LogDriver=none`: systemd already captures the
container's stdout into the journal under its own unit, and Podman's journald
driver would write a second copy of every structured line. The long-running
containers keep `LogDriver=journald` deliberately, because `podman logs` is
worth having for them.

There is no `OnFailure=` alert unit, unlike orbit-data — that project's alert
path is a subcommand of its own application, and this one has no equivalent
yet. Until it does, failures surface through `systemctl --failed` and the
journal. Wiring an alert to the operator CLI is a reasonable Phase 2 follow-up.

`systemctl --failed` is only worth watching if it is empty when nothing is
wrong, so a clean `systemctl stop` has to leave it clean. That is what the
`NoNewPrivileges` note below is protecting.

### Why two units do not set `NoNewPrivileges=true`

`sre-tab-db.container` and `sre-tab.container` are the two, and neither is an
oversight. On Debian 13 with podman 5.4.2, setting `no_new_privs` on a
container blocks the AppArmor profile transition that `crun` performs on exec,
leaving the container's processes split across `containers-default-<ver>` and
its `//&crun` sub-profile — and AppArmor then denies signals between them:

```
apparmor="DENIED" operation="signal" profile="containers-default-0.62.2"
  comm="postgres" requested_mask="send" signal=usr1
  peer="containers-default-0.62.2//&crun"
```

Both units need signals to work. PostgreSQL's entrypoint drops privilege with
`gosu` and its postmaster wakes backends with `SIGUSR1`; uvicorn re-raises the
signal it caught so its exit status reports a clean shutdown. With the flag
set, the database never finishes starting (five minutes, then
`TimeoutStartSec`) and every application stop exits 1 and lands in
`systemctl --failed`.

An init process does not help — it needs the same signal delivery, and podman's
own `--init` makes it worse, turning each stop into a 30-second hang ending in
`SIGKILL`. Each unit's comment carries the full flag matrix. In place of the
flag, the application image strips every setuid and setgid bit at build time,
which removes the escalation rather than disarming it; `DropCapability=all`,
`User=`, and the read-only rootfs are unchanged on both. Every other unit in
the stack still sets `NoNewPrivileges=true`.

If a future podman, kernel, or AppArmor policy fixes the transition, both flags
can come back — check by starting the database with the flag and watching for
`PostgreSQL init process complete` within a few seconds.

## Database major-version upgrades

`postgres:18` moved `PGDATA` to `/var/lib/postgresql/18/docker` and declares
its volume one level up, so `sre-tab-db.container` mounts
`/var/lib/postgresql` — which would have been wrong on `postgres:17`. Read
that line before changing the pin, and take a backup first. Renovate holds
major bumps on the dependency dashboard rather than opening a PR, for exactly
this reason.
