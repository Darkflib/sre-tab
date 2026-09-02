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
                 │     ├── sre-tab-backup   (oneshot, uid 999)  │
                 │     └── sre-tab-prune-sessions               │
                 │             (timer, oneshot, uid 10001)      │
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

<!-- docs:run -->
```bash
sudo deploy/install.sh
```

Idempotent. It installs the Quadlets to `/etc/containers/systemd`, the
maintenance timers to `/etc/systemd/system`, and the Caddyfile and backup
script to `/etc/sre-tab`. It creates `/srv/sre-tab/backups` owned by `999:999`, mode
`0700`.

Those ids mean `postgres` *inside the postgres image*, not on the host —
**on Debian 13 gid 999 is `systemd-journal`**, a group operators do add people
to. Hence `0700` and not `0750`, and hence `umask 077` in `backup.sh`: a dump
is a complete copy of every user record, and neither the directory mode nor
the file mode should be the only thing protecting it. Nothing legitimate needs
group access — the backup container writes as uid 999 and `restore.sh` runs as
root.

One thing that will look like a contradiction if you meet it before you have
read the rest of this document: with off-host backups configured, the
installer adds a POSIX ACL granting one named user read access, and `ls` then
shows the directory as `drwxr-x---+` and the dumps as `-rw-r-----+` rather
than the `0700`/`0600` described above. **That is not group access.** The group
bits of a file carrying an ACL are the ACL *mask*, not `group::`, which stays
`---`; a member of gid 999 is still refused. `getfacl` is the only reading of
those permissions that means anything, and
[How an unprivileged unit reads a 0700 directory](#reading-the-dumps) has the
measurement.

It seeds `/etc/sre-tab/app.env` from `deploy/app.env.example` **once** and
never overwrites it afterwards. Everything else it installs is replaced on
every run: keep intentional changes in the repository, not in `/etc`.

Then set the configuration. `APP_BASE_URL`, `GITHUB_REDIRECT_URI`,
`GITHUB_CLIENT_ID`, and `ALLOWED_GITHUB_IDS` all have to be set. By hand:

```bash
sudoedit /etc/sre-tab/app.env
```

or non-interactively, which is what a configuration-management run does and
what this document's own CI execution does:

<!-- docs:run -->
```bash
sudo sed -i \
  -e "s|^APP_BASE_URL=.*|APP_BASE_URL=${APP_BASE_URL:?}|" \
  -e "s|^GITHUB_REDIRECT_URI=.*|GITHUB_REDIRECT_URI=${APP_BASE_URL:?}/api/v1/auth/github/callback|" \
  -e "s|^GITHUB_CLIENT_ID=.*|GITHUB_CLIENT_ID=${GITHUB_CLIENT_ID:?}|" \
  -e "s|^ALLOWED_GITHUB_IDS=.*|ALLOWED_GITHUB_IDS=${ALLOWED_GITHUB_IDS:?}|" \
  /etc/sre-tab/app.env
```

`ALLOWED_GITHUB_IDS` is in that list, and the next section is about what
happens when it is left out.

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

**Every** installation starts closed, including one seeded straight from
`deploy/app.env.example`. That template used to ship the upstream project's
own operator allow-list, so a fresh install worked out of the box — and so
did a fresh install by somebody else, because GitHub user IDs are global
rather than scoped to an OAuth application. Registering your own OAuth app
and replacing every credential in the file did not revoke the three accounts
named in it; only noticing the line and editing it did. Filling this in is
now a step, which is the correct trade: a deployment that refuses its own
operator on the first sign-in is a five-minute problem, and one that silently
admits three strangers is not. Check it before concluding the OAuth app is
misconfigured:

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
| `sre-tab-database-url` | `DATABASE_URL` | app, migrations, session sweep |
| `sre-tab-session-secret` | `SESSION_SECRET` | app |
| `sre-tab-github-client-secret` | `GITHUB_CLIENT_SECRET` | app |

The database password appears inside `sre-tab-database-url` as well as in
`sre-tab-postgres-password`, and a mismatch between the two is a tedious way
to lose an afternoon. `create-secrets.sh` generates it once and writes both,
so they cannot disagree:

<!-- docs:run -->
```bash
sudo deploy/scripts/create-secrets.sh < "${GITHUB_CLIENT_SECRET_FILE:?}"
```

`GITHUB_CLIENT_SECRET_FILE` is the file holding the secret — named as a
variable rather than written as `/path/to/…` so that this document can be
executed rather than proofread. The secret is read from standard input, never
from an argument or an environment variable: argv is visible to every process
on the host, and the variable above holds a *path*, not the secret itself.
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

<!-- docs:run -->
```bash
sudo deploy/install.sh --start
```

`--start` refuses to proceed if any of the four secrets is missing. It enables
every timer under `deploy/systemd` — the backup and the session sweep — and
restarts all five long-running units in a single `systemctl` transaction,
which is what makes systemd resolve the ordering between them rather than
starting them in the order typed.

The timers are enumerated from the directory rather than listed by name.
A timer that the installer stages but never enables is installed, inert, and
indistinguishable from a working one until the thing it was meant to bound has
already grown without bound.

Verify. Note the wait: `systemctl` returning is not the all-clear, for the
reasons measured under [Upgrading](#how-long-a-deploy-actually-takes), so
polling is the correct check rather than a single request:

Every request is bounded, including the ones after the loop. That is not
belt-and-braces: the state described under Upgrading is a listener that
*accepts* a connection and never answers, so a `curl` without `--max-time`
against it waits indefinitely rather than failing.

<!-- docs:run -->
```bash
systemctl status --no-pager sre-tab-db.service sre-tab.service sre-tab-web.service

ready=false
for _ in $(seq 1 60); do
    if curl --fail --silent --max-time 5 --output /dev/null \
            http://127.0.0.1:8080/api/v1/healthz; then
        ready=true
        break
    fi
    sleep 2
done
if [ "$ready" != true ]; then
    echo "healthz did not answer within two minutes" >&2
    exit 1
fi

curl --fail --silent --max-time 5 http://127.0.0.1:8080/api/v1/healthz
curl --fail --silent --max-time 10 --output /tmp/sre-tab-index.html http://127.0.0.1:8080/
head -5 /tmp/sre-tab-index.html
```

Quadlet services are transient generated units and cannot be enabled with
`systemctl enable`; their `[Install]` sections are applied by the generator at
boot and on `daemon-reload`, so starting them explicitly is enough. The timers
are native units and are enabled normally.

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

An upgrade is a commit, not a restart. Every unit that runs the application
image pins `ghcr.io/darkflib/sre-tab:sha-<commit>@sha256:<digest>` with
`Pull=missing`,
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
that digest, and only then rewrites every unit named in its `UNITS` list. If
verification fails it writes nothing:

```
==> cosign verify ghcr.io/darkflib/sre-tab@sha256:…
Error: no signatures found
```

Review and commit the result — the diff is one `Image=` line per unit, and
they must always move together, because migrations, the application, the
frontend assets, and the session sweep ship in one image so they cannot skew.
CI enforces both that they agree and that the digest they name is signed: it
counts distinct references rather than checking a fixed number, so adding a
unit needs no edit there. `UNITS` in `promote.sh` is the one list that does
need it — a unit missing from it is not slow drift but the next promotion
failing that check outright.

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

Two units are deliberately absent from that list, for opposite reasons.
`sre-tab-web.service` is Caddy and does not run the application image, so a
promotion never changes it. `sre-tab-prune-sessions.service` does run it, but
it is timer-driven and not running, so there is nothing to restart — it
adopts the new digest by itself at its next elapse, once `install.sh` has
staged the rewritten unit. So the four units a promotion rewrites and the
four services this command restarts are different sets of four, overlapping
in three.

Take a backup before any upgrade that carries a migration; `alembic
downgrade` is not a substitute for a restore.

<a id="how-long-a-deploy-actually-takes"></a>
### How long a deploy actually takes

This used to say "a sub-second blip while Caddy restarts". That was wrong by
a factor of about forty, and wrong about the mechanism, which is the part
worth reading. Measured on Debian 13 with podman 5.4.2, polling
`/api/v1/healthz` every 100ms across the four-unit restart above:

| | Before | After |
| --- | --- | --- |
| `systemctl restart` returns | 35.6s | 15.4s |
| Service unreachable | 43.7s | 36.1s |
| …of which is *after* `systemctl` returned | 8.3s | 20.9s |

The application itself is not the slow part: it stops answering for **0.5s**
and is serving again **2.8s** after the restart begins. Restarting
`sre-tab.service` alone showed systemd waiting a further **32s** after the
application was already answering.

That wait was `Notify=healthy` gating on the image's healthcheck, whose first
run comes one whole interval after start regardless of `--start-period`. The
interval was 30s in the image while `sre-tab-db.container` had used
`HealthInterval=10s` in its unit all along — the two definitions live in
different files, which is how they drifted apart unnoticed. The image is now
10s too, which is what moves the first row of that table.

**A deploy is not over when `systemctl` returns.** It is the second row that
matters to anyone watching, and lowering the healthcheck interval does not
address it: the service stays unreachable for roughly 20s *after* the command
comes back, while Caddy's own log shows it serving 50ms after its container
started. So it is neither Caddy booting nor the application.

Two things serve `127.0.0.1:8080`, which is what makes this confusing to
diagnose. netavark installs a hostport rule —
`ip daddr 127.0.0.1 tcp dport 8080 dnat ip to 10.89.61.20:8080` — and podman
separately holds a reservation listener on the same port so nothing else can
claim it, visible as `conmon` in `ss -lntp`. Probing across a restart walks
through three states: `refused` while both are gone, then **accepted and then
hung** — the reservation listener takes the connection and never forwards it,
because the DNAT rule is not back yet — and finally serving. The tail is that
middle state, which is why it looks like black-holing from the client and why
none of the three services' logs mention it.

Whether the ordering can be fixed from the unit files, or is podman's to fix,
is not yet established.

The measurements were taken from the host's own loopback, which was first
written up as a caveat — the tail might be a loopback-and-DNAT artefact a
real client would not see. **It is not a caveat, it is the client path.**
`sre-tab-web.container` publishes `127.0.0.1:8080:8080` deliberately, because
TLS is terminated by the host's existing proxy, and that proxy reaches Caddy
over loopback exactly as the polling did. There is no off-host client of this
port to compare against, so nothing insulates users from the tail: it is what
the outer proxy sees, and it reaches them as 502s for the duration.

One caveat does stand: this is a 4-core cloud instance, so the absolute
figures are that host's, not a constant.

The practical consequence is unchanged by any of it: **wait for `healthz` to
answer rather than treating the prompt returning as the all-clear.** The
verification step under *First start* is the right check to run, and running
it immediately after `systemctl restart` will fail.

None of this is a defect for a single-instance self-hosted service, which
takes downtime on deploy by design. If it ever needs to be zero-downtime, the
pattern to reach for is orbit-data's atomic release symlink rather than a
destructive copy — and the tail above would need root-causing first, because
it is the larger half.

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

<!-- docs:run -->
```bash
sudo systemctl stop sre-tab-web.service sre-tab.service sre-tab-db.service
sudo podman network rm systemd-sre-tab
sudo deploy/install.sh --start
```

Quadlet names the network `systemd-` plus the unit name, hence
`systemd-sre-tab` rather than `sre-tab`. Confirm the result — and note that
the range must start above Caddy's pinned `.20`, or the address-collision fix
is inert:

<!-- docs:run -->
```bash
sudo podman network inspect systemd-sre-tab \
    --format '{{range .Subnets}}subnet={{.Subnet}} gateway={{.Gateway}} range={{.LeaseRange}}{{end}}'
sudo podman network inspect systemd-sre-tab | grep -q '"start_ip": "10.89.61.32"'
```

An upgrade that skips this leaves the old, rangeless network in place and the
fix inert.

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

- **Not strip or rewrite** `Content-Security-Policy`,
  `Strict-Transport-Security`, `X-Content-Type-Options`, `Referrer-Policy`,
  `X-Frame-Options`, `Cross-Origin-Opener-Policy`,
  `Cross-Origin-Resource-Policy`, or `Permissions-Policy`. Several proxies
  helpfully add their own CSP; two `Content-Security-Policy` headers are
  intersected by the browser, and the result is usually a broken page.
- **Not serve the site to clients over plain HTTP.** Forwarding to Caddy on
  `127.0.0.1:8080` over HTTP is the design and is expected; what must not
  happen is a browser reaching the site over HTTP.
  `Strict-Transport-Security: max-age=31536000` is set by the application and
  by Caddy, but a browser only honours it on a response it received over
  HTTPS — and both of those layers sit behind the outer proxy on plain HTTP.
  That proxy is therefore the only thing deciding whether the header means
  anything at all.
- **Not add its own `Cache-Control`.** `index.html` is deliberately
  `no-store` and `/assets/*` deliberately `immutable`; overriding either is
  how a deployment lands and nobody sees it.
- **Set `X-Forwarded-For` itself, and refuse a client-supplied one.** Caddy is
  the inner of two proxies. If the outer one does not populate it, every
  request reaches the application looking as though it came from the proxy,
  and per-IP rate limiting silently becomes global rate limiting.
`includeSubDomains` is deliberately **not** set. On the documented topology —
a dedicated host such as `news.example.com` — adding it is correct and worth
doing, in `app/middleware.py` and in the mirrored block in `Caddyfile`
together, since `deploy/scripts/check-header-parity.sh` fails CI if only one
of them changes. On an apex deployment it is a trap: it forces HTTPS on every
other subdomain of that registrable domain, for `max-age` seconds, and cannot
be withdrawn early from the server side. Decide which of those two you are
before adding it.

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

The sweep in [Session retention](#session-retention) is scheduled after this
window on purpose, so a session row deleted at 04:17 is still present in the
dump taken at 03:22.

**`/srv/sre-tab/backups` is on the same host as the database.** That is a
backup, not disaster recovery. [Off-host backups](#off-host-backups) below
copies each night's dump to another host, an object store, or both, and
verifies it where it landed.

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

<a id="restore"></a>
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

<a id="off-host-backups"></a>
## Off-host backups

Everything above keeps the dumps on the machine that holds the database. This
copies the newest one somewhere else and then asks that somewhere else what it
is holding.

That second half is the point. An upload command exiting zero is a statement
about the transfer, not about the bytes at the far end, and an off-host copy
nobody has ever verified is worse than none — it is the belief that you have
disaster recovery. Both transports below end by re-deriving the checksum where
the file landed and comparing it against the one computed here.

It is **off by default**, and the switch is the existence of one file. Without
`/etc/sre-tab/backup-offsite.env` the unit's `ConditionPathExists=` skips it
entirely; with it and no targets, it fails loudly rather than reporting
success having done nothing.

```bash
sudo install -m 0600 -o root -g root \
    /etc/sre-tab/backup-offsite.env.example /etc/sre-tab/backup-offsite.env
sudo "$EDITOR" /etc/sre-tab/backup-offsite.env
sudo systemctl start sre-tab-backup-offsite.service    # run it now
journalctl -u sre-tab-backup-offsite.service -n 40
```

### One list of URLs, and the scheme picks the transport

```ini
BACKUP_OFFSITE_TARGETS="ssh://sre-tab-offsite@backup.example.net:22/srv/offsite s3://my-bucket/sre-tab"
```

A separate mode setting beside a separate address would let you configure an
ssh mode with an S3 address; this way that configuration cannot be written
down. Both at once needs no extra syntax, and a belt-and-braces operator
wanting a copy in each place is exactly who this is for. Every target is
attempted even after one fails, so an unreachable ssh host does not silently
cancel tonight's upload to the object store — the exit status is the summary.

Multiple targets of the same scheme share one set of credentials.

### It is caused by a successful backup, not scheduled beside one

`deploy/systemd/sre-tab-backup.service.d/50-offsite.conf` adds
`OnSuccess=sre-tab-backup-offsite.service` to the backup unit. So the copy runs
when a backup has just succeeded and does not run when one has just failed —
no second `OnCalendar=` to keep in step with the backup's 03:22 start and its
20 minutes of jitter, and no night on which a failed backup is followed by
cheerfully re-copying yesterday's dump and exiting zero.

`Requisite=sre-tab-backup.service` is the obvious way to write this and does
not work. `sre-tab-backup.service` is a oneshot without `RemainAfterExit`, so
it is `inactive (dead)` the instant it succeeds, and `Requisite=` refuses to
start against an inactive unit. Measured on Debian 13 with systemd 257: with
the backup having *just* completed with `Result=success`, a `Requisite=`
dependant fails with "Dependency failed" and never runs. That gate would have
reported nothing wrong while copying nothing, indefinitely.

**The trade-off, because you will meet it:** the copy has no timer of its own,
so `systemctl list-timers` will never show it. Its evidence is the unit:

```bash
systemctl status sre-tab-backup-offsite.service
journalctl -u sre-tab-backup-offsite.service --since -7d
```

A `.env` naming targets it cannot reach fails the unit, and the unit carries
`OnFailure=sre-tab-alert@%n.service` so that failure reaches whatever the host
uses to reach a person. A silently failing off-host copy is precisely the
failure this feature must not have.

The script also refuses a dump older than `OFFSITE_MAX_DUMP_AGE_HOURS` (48).
A stale dump means the pipeline has stopped somewhere the copier cannot see —
the timer disabled, the database container gone, the host's clock wrong — and
copying it while exiting zero would report a healthy pipeline.

### The one uncontainerised unit in the deployment

It runs on the host, as `sre-tab-offsite`, because the backup itself runs
inside the pinned postgres image with `ReadOnly=true` and `DropCapability=all`
and that image has no ssh client and no S3 client. Putting one in it would
mean maintaining a derived image of the database server in order to move a
file; a second image would mean a second registry dependency. `curl`, `openssl`,
`ssh`, and coreutils are already on any host that can run this deployment.

It pays for being outside a container with the sandbox in the unit file:
`ProtectSystem=strict`, an empty `CapabilityBoundingSet=`, `NoNewPrivileges`,
`PrivateTmp`, `MemoryDenyWriteExecute`, `SystemCallFilter=@system-service`,
`RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, `ReadOnlyPaths=` over the
backup directory and one writable state directory. Check rather than believe:

```bash
systemd-analyze security sre-tab-backup-offsite.service
```

which reports **1.5 OK** on the reference host. (The containerised units score
9.6–9.8 there; that number describes the `podman` client process systemd can
see, not the sandbox inside the container, and it is not a comparison worth
drawing.)

`PrivateTmp=true` is set deliberately and is not free elsewhere: it gives a
private `/tmp` **and** `/var/tmp`, both discarded when the unit exits, so a
tool that spools uploads there loses them with a zero exit status. Nothing on
this path spools — `curl` streams the dump straight from the file — which is
one of the reasons the AWS CLI is not what the script calls. See
[Why not the AWS CLI](#why-not-the-aws-cli).

<a id="reading-the-dumps"></a>
### How an unprivileged unit reads a 0700 directory

`/srv/sre-tab/backups` is `0700` owned by uid 999, and that mode is
load-bearing: gid 999 is `systemd-journal` on Debian 13, a group operators do
add people to. So `sre-tab-offsite` cannot read a thing in there by default.

`install.sh` grants exactly one extra reader with a POSIX ACL:

```bash
setfacl    -m u:sre-tab-offsite:rx /srv/sre-tab/backups
setfacl -d -m u:sre-tab-offsite:r  /srv/sre-tab/backups
```

The default ACL is the half that is easy to miss. New dumps are created by the
backup container under `umask 077`; when a directory carries a default ACL the
umask is ignored and the default supplies the mode instead, so each night's
dump comes out with `user::rw-`, `group::---`, `other::---`, and one named
entry. Measured on Debian 13 and ext4: a member of gid 999 still gets `EACCES`
on both the directory and the files.

`ls -ld` then shows `drwxr-x---+`, which *looks* like group access and is not —
the group bits of a file carrying an ACL are the mask, not `group::`. Read the
ACL, not the mode:

```bash
getfacl /srv/sre-tab/backups
```

The alternative was `AmbientCapabilities=CAP_DAC_READ_SEARCH`, which would
have granted this unit read access to every file on the host — `/etc/shadow`
and the podman secrets included — in order to read two.

**Any `chmod` of that directory silently breaks it.** `chmod` rewrites the ACL
mask from the group bits, so `install -d -m 0700` sets the mask to `---` and
masks the grant out while leaving the named entry visible in `getfacl` with
`#effective:---` beside it. This happened during development, on a host where
a second checkout re-ran an older `install.sh`. Re-running the installer is
both how it breaks and how it is fixed, and the script says so rather than
reporting "no dump in this directory", which is what it would otherwise look
like from the inside.

Needs `acl` installed (`apt-get install acl`); the installer warns rather than
failing if it is not.

<a id="ssh-transport"></a>
### ssh

The far end runs `deploy/scripts/backup-offsite-receive.sh` as a **forced
command**, so the sending key has no shell there and four verbs are its entire
vocabulary. Copy that script to the receiving account and paste one line into
its `authorized_keys`:

```bash
# on the receiving host, as the receiving account
mkdir -p ~/bin ~/sre-tab-offsite
install -m 0500 backup-offsite-receive.sh ~/bin/backup-offsite-receive.sh
```

```
command="/home/sre-tab-offsite/bin/backup-offsite-receive.sh",restrict ssh-ed25519 AAAA... sre-tab off-host backup
```

`restrict` removes port, agent and X11 forwarding, the pty, and `~/.ssh/rc`;
the forced command means whatever the client asks for lands in
`SSH_ORIGINAL_COMMAND` and this script runs instead. On the sending host:

```bash
sudo ssh-keygen -t ed25519 -N '' -C 'sre-tab off-host backup' \
    -f /etc/sre-tab/backup-offsite.key
sudo chown sre-tab-offsite:sre-tab-offsite /etc/sre-tab/backup-offsite.key
sudo chmod 0400 /etc/sre-tab/backup-offsite.key
sudo cat /etc/sre-tab/backup-offsite.key.pub          # paste this into authorized_keys

# Pin the far end's host key. Trust-on-first-use would hand the dumps to
# whoever answered on the night the file was empty, so compare the fingerprint
# against the far host itself before relying on it.
ssh-keyscan -p 22 backup.example.net \
    | sudo tee /etc/sre-tab/backup-offsite.known_hosts
```

**The sending host has no verb that deletes, and that is the design.** A
machine running a live database is the machine an attacker reaches first, and
a backup target it can erase is not disaster recovery, it is a second thing to
lose in the same incident. So retention at the far end is the far end's own
decision, taken by the receiving script after it has verified the new dump on
its own disk — never before, so a night on which the transfer failed is not
also the night the oldest good copy was dropped. Set the window there:

```sh
# /etc/sre-tab-offsite-receive.conf on the receiving host, if the defaults
# ($HOME/sre-tab-offsite and 14 days) are not what you want
RECEIVE_DIR=/srv/offsite
RECEIVE_KEEP_DAYS=14
```

`put` also refuses to overwrite a name that already exists, which is the ssh
analogue of the Object Lock recommendation below: yesterday's good dump cannot
be replaced by anything holding this key. The sidecar is sent first so the
receiver can check the dump against it *before* publishing it, which means an
interrupted transfer leaves only a `.partial` for the receiver to sweep and
the next run repairs itself rather than being locked out by its own
append-only rule.

The directory in the target URL travels with every verb and the far end
compares it against its own configuration, refusing anything else. It is an
assertion that gets checked, not a destination the far end obeys — a mistyped
URL fails on the first run instead of quietly filling somewhere unexpected.

A forced `rsync --server` or `scp -t` was the other option. Those move bytes
and nothing else, and this feature is not about moving bytes: verifying means
executing something at the far end, and bolting a second unrestricted key onto
the account for "just run `sha256sum`" hands back the arbitrary-command
surface the forced command exists to remove.

One trap worth knowing if the receiving account is created with `useradd` and
the far end's sshd runs `UsePAM no`: a locked password (`!`) is refused even
for public-key authentication, with "account is locked" in the log. `usermod -p
'*' <account>` is the key-only state you want.

<a id="s3-transport"></a>
### S3, and S3-compatible

`OFFSITE_S3_ENDPOINT` is a first-class setting rather than an escape hatch:
self-hosters reach for MinIO, Garage, Backblaze B2, Wasabi, and Hetzner far
more often than for AWS proper. Setting it also selects path-style addressing,
because those implementations need a wildcard DNS record and a matching
certificate before they will answer to `bucket.host`. Leave it empty for AWS
and the request is virtual-hosted.

```ini
OFFSITE_S3_ENDPOINT=https://s3.eu-central-003.backblazeb2.com
OFFSITE_S3_REGION=eu-central-003
OFFSITE_S3_ACCESS_KEY_ID=...
OFFSITE_S3_SECRET_ACCESS_KEY=...
```

**What is compared, and why it is sound.** The upload carries
`x-amz-checksum-sha256`, so the store recomputes SHA-256 over the bytes it
received and rejects the `PUT` outright if they disagree — a truncated body
never becomes an object. Then a separate `HEAD` with
`x-amz-checksum-mode: ENABLED` asks the store for the SHA-256 it has recorded
against the stored object, which is a different question from "did the PUT
return 200", and that value is compared against the digest computed locally.
Two independent checks, and the second one still catches bit-rot in the store
after the write.

**Not the ETag.** For a single-part upload the ETag happens to be the MD5 of
the body, but for a multipart upload it is an MD5 of concatenated part MD5s
with a part count appended — so an ETag comparison works until the dump gets
big and then quietly stops meaning anything. For the same reason the script
refuses a dump over the 5 GiB single-`PUT` limit rather than switching to
multipart, whose `x-amz-checksum-sha256` is a checksum *of the part checksums*
and not of the object. Use an `ssh://` target for a database that large.

If a store returns no `x-amz-checksum-sha256` at all, the run fails. A missing
answer is not a passing one.

<a id="an-iam-policy-that-cannot-delete"></a>
### An IAM policy that cannot delete, and Object Lock

A backup target reachable with full credentials from the machine being backed
up is a ransomware target, not disaster recovery. Scope the credential to
writing new objects under one prefix and reading them back to verify — nothing
else, and in particular not `s3:DeleteObject`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "WriteBackupsAndReadThemBackToVerify",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": ["arn:aws:s3:::my-bucket/sre-tab/*"]
    },
    {
      "Sid": "ListOnlyThatPrefix",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": ["arn:aws:s3:::my-bucket"],
      "Condition": { "StringLike": { "s3:prefix": ["sre-tab/*"] } }
    }
  ]
}
```

`s3:GetObject` is what `HEAD` needs; without it the verification cannot happen
and the whole exercise is pointless. `s3:ListBucket` is only needed for
client-side retention, which is off by default — drop that statement if you
leave it off.

**Turn on versioning and Object Lock in compliance mode.** Then a compromised
host cannot destroy what it has already sent, whatever it manages to do to the
credential: in compliance mode the retention cannot be shortened or removed by
anyone, including the root account, for its duration.

```bash
aws s3api create-bucket --bucket my-bucket --object-lock-enabled-for-bucket
aws s3api put-object-lock-configuration --bucket my-bucket \
    --object-lock-configuration \
    'ObjectLockEnabled=Enabled,Rule={DefaultRetention={Mode=COMPLIANCE,Days=30}}'
```

Object Lock requires versioning and can only be enabled **at bucket creation**
on AWS, so this is a decision to take before the first upload rather than
after the first incident. Support elsewhere varies and is worth checking
rather than assuming: MinIO implements it (bucket must be created with it
enabled), Backblaze B2 and Wasabi both offer object lock through their S3
APIs, and several smaller S3-compatible services implement versioning without
it. Where it is genuinely unavailable, versioning alone still means an
overwrite does not destroy the previous version.

**Retention, and why the default is to leave it to the bucket.** A lifecycle
rule expiring objects under the prefix after `BACKUP_KEEP_DAYS` needs no
delete permission on the host holding the database, which is the entire point
of the paragraph above. So `OFFSITE_S3_PRUNE` defaults to `false`. Turn it on
only for a store with neither lifecycle rules nor Object Lock, and add
`s3:DeleteObject` on the same prefix when you do; the pass only ever considers
objects whose names match the published `<database>-<stamp>.dump[.sha256]`
scheme, so nothing else parked under that prefix is at risk.

<a id="why-not-the-aws-cli"></a>
### Why not the AWS CLI

`deploy/scripts/backup-offsite.sh` signs its own requests with `curl` and
`openssl` rather than calling `aws`. Debian 13 does package AWS CLI v2 and it
works — it was used during development as a reference implementation to check
that the objects this script writes are ones a real client agrees about — but
`apt-get install awscli` pulls **23 packages and 144MB**, including a second
Python and a cryptography stack, onto a host whose entire design is that
everything runs in a container with all capabilities dropped. It also spools,
which is a trap next to `PrivateTmp=true`. `curl` and `openssl` are already
here.

The failure mode of a signing mistake is the safe one: a wrong signature is
refused by the store, so the run fails loudly and non-zero. There is no path
by which a signing bug produces a copy that is *believed* verified.

The honest limitation: the signer has been exercised against MinIO, not
against AWS proper. Path-style and virtual-hosted addressing are both
implemented; only path-style has been run.

One weakness, stated because the policy above is what contains it: `openssl`
accepts an HMAC key only on its command line, so the *derived* per-day signing
key is briefly visible in `/proc/<pid>/cmdline`. The secret itself never is. A
local unprivileged reader who catches the derived key gets, at worst, the
ability to write objects under one prefix of one bucket for the rest of the
UTC day — and with Object Lock on, nothing it can do to what is already there.

### Restoring from the off-host copy

A backup you cannot restore from is not a backup, and the off-host copy is no
exception. Bring the dump *and its sidecar* back to the host, then use the
same [`restore.sh`](#restore) as always — it verifies the sidecar before it
drops anything, which is exactly the check that matters after a file has
crossed a network.

From an ssh target, pulling with your own account rather than the restricted
backup key (which cannot read):

```bash
scp 'backup.example.net:/srv/offsite/sretab-20260817T032200Z.dump*' /srv/sre-tab/backups/
sudo chown 999:999 /srv/sre-tab/backups/sretab-20260817T032200Z.dump*
sudo chmod 0600   /srv/sre-tab/backups/sretab-20260817T032200Z.dump*
sudo deploy/scripts/restore.sh /srv/sre-tab/backups/sretab-20260817T032200Z.dump
```

From S3:

```bash
aws s3 cp s3://my-bucket/sre-tab/sretab-20260817T032200Z.dump        /srv/sre-tab/backups/
aws s3 cp s3://my-bucket/sre-tab/sretab-20260817T032200Z.dump.sha256 /srv/sre-tab/backups/
sudo chown 999:999 /srv/sre-tab/backups/sretab-20260817T032200Z.dump*
sudo chmod 0600   /srv/sre-tab/backups/sretab-20260817T032200Z.dump*
sudo deploy/scripts/restore.sh /srv/sre-tab/backups/sretab-20260817T032200Z.dump
```

The `chown` is not incidental. `restore.sh` checks the checksum inside a
container as uid 999 precisely because the dumps are 0600 and owned by that
uid; a file you have just downloaded as yourself is unreadable to it, and the
resulting `EACCES` used to be reported as a checksum mismatch — which points
at data corruption when the real problem is the reader.

### Testing the path by hand

```bash
# Take a backup and let OnSuccess= pull the copy behind it.
sudo systemctl start sre-tab-backup.service
journalctl -u sre-tab-backup-offsite.service -n 40

# Or just the copy, against whatever dump is newest.
sudo systemctl start sre-tab-backup-offsite.service

# Prove the verification rejects something. Corrupt one byte at the far end
# and run it again: it must fail, and name the file.
ssh backup.example.net \
  'dd if=/dev/zero of=/srv/offsite/sretab-*.dump bs=1 seek=1000000 count=1 conv=notrunc'
sudo systemctl start sre-tab-backup-offsite.service   # must exit non-zero

# What the far end is actually holding, from the sending host, using the
# restricted key and nothing else.
sudo -u sre-tab-offsite ssh -F none -i /etc/sre-tab/backup-offsite.key \
    -o IdentitiesOnly=yes -o UserKnownHostsFile=/etc/sre-tab/backup-offsite.known_hosts \
    sre-tab-offsite@backup.example.net 'list /srv/offsite'
```

Do that corruption test once on a host you care about. A checksum comparison
that has never rejected anything is not a checksum comparison, and this
project has shipped six gates that reported success while verifying nothing.

### What has been exercised, and where

On a Debian 13 host with podman 5.4.2 and systemd 257: both transports end to
end, including a real `pg_dump` from a running stack copied to both targets in
one run and verified at each; a single flipped byte at the ssh far end and a
truncated object in the store, each rejected; the restricted key refused a
shell, an unknown verb, a directory it was not configured for, a filename
outside the published scheme, and an overwrite of an existing dump; far-end
retention removing only matching names and leaving an operator's own files and
another database's dumps alone; `OnSuccess=` firing on a successful backup and
not firing on a failed one; and `OnFailure=` starting the alert template.

Not exercised: AWS proper (MinIO stood in), virtual-hosted addressing, Object
Lock on any implementation other than MinIO, and a far end that is genuinely a
different machine — the ssh tests ran against a second sshd and a separate
account on the same host, which is a real ssh connection and not a real
network.

<a id="session-retention"></a>
## Session retention

`sre-tab-prune-sessions.timer` runs daily at 04:17 UTC with up to 10 minutes of
jitter, and `Persistent=true` so a host that was off overnight sweeps on the
way back up rather than waiting another day. It runs
`sre-tab sessions prune` in the application image.

**Without it the `sessions` table grows forever.** Nothing else deletes from
it: sign-in inserts a row, logout sets `revoked_at`, and every read filters on
those columns. Sign-in also rotates — it revokes the previous session and
inserts a new one — so rows accumulate at the rate people open the app, not
the rate they remember to log out.

Two classes of row are swept, and not at the same moment:

| Row | Deleted |
| --- | --- |
| Expired, never revoked | As soon as `expires_at` passes |
| Revoked | Seven days after `revoked_at` |

The grace period on revoked rows is deliberate. `revoked_at` is the only trace
this system keeps that a logout — or a rotation, which revokes the same way —
happened at all, and deleting it on the spot means "when did this session end,
and did it end deliberately or by expiry?" has no answer during the week
someone is most likely to ask it. The row is inert throughout: a revoked
session cannot authenticate at any point in that window. Seven days is shorter
than the default `SESSION_TTL_DAYS` of 14, so the grace period never keeps a
row longer than leaving it alone would have.

A row that is both revoked *and* expired is held by the grace period rather
than swept by the expiry rule.

Why 04:17 and not something nearer the backup: the backup starts at 03:22 with
up to 20 minutes of jitter, so its window closes at 03:42, and a `pg_dump`
holding a snapshot while a `DELETE` runs against the same database is worth
avoiding on a single-host deployment where both compete for one disk. Running
after it is also the useful order — the night's dump is taken before the
sweep, so a row deleted at 04:17 is still in the most recent backup for a full
day afterwards.

```bash
systemctl list-timers 'sre-tab-*'
systemctl start sre-tab-prune-sessions.service    # sweep now
journalctl -u sre-tab-prune-sessions.service --since today
```

The job prints what it deleted and exits zero whether or not it deleted
anything — an empty sweep is the steady state on a quiet instance, not a
condition worth waking anyone for. A failure surfaces the usual way, in
`systemctl --failed`.

To run it by hand against a different retention window, or to keep nothing at
all:

```bash
podman exec sre-tab-app sre-tab sessions prune --revoked-grace-days 30
podman exec sre-tab-app sre-tab sessions prune --revoked-grace-days 0
```

It is safe at any time and takes no locks worth the name: a live session is
never a candidate, so the sweep cannot sign anybody out.

**Unlike the backup, this unit runs the application image**, so its digest is
one of the four `deploy/scripts/promote.sh` moves together and CI checks for
agreement. It is not in the restart list after a promotion, and does not need
to be: it is timer-driven rather than running, so it picks up the new digest at
its next elapse once `install.sh` has staged the unit.

<a id="seeding-the-catalogue-and-the-operator-cli"></a>
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
