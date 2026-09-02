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
                 │        │ 127.0.0.1:8080 (default)            │
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

Four of those units talk to PostgreSQL, and each does so as a different,
deliberately limited role: the application and the session sweep as
`sretab_app` (DML only), the migration unit as `sretab_migrate` (DDL, and it
owns every table), the backup as `sretab_readonly`. None of the three is a
superuser, and none can `COPY … TO PROGRAM`. Only `sre-tab-db` itself holds a
superuser credential, because it has to own the cluster.
[deploy/ROLES.md](ROLES.md) is why, and the
[runbook below](#cutting-a-running-deployment-over-to-the-roles) is how to get
an existing deployment there.

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

Two packages, and the second is the one that gets missed:

<!-- docs:run -->
```bash
sudo apt-get update
sudo apt-get install -y podman aardvark-dns
```

`aardvark-dns` is a *Recommends* of Debian's `podman`, not a dependency, so a
host built with `--no-install-recommends` — or one whose apt configuration
turns recommends off across the board — ends up with podman, with netavark,
and with no container DNS at all. **Nothing announces it.** Podman says so
once, as a warning, and carries on:

```
level=warning msg="aardvark-dns binary not found, container dns will not be enabled"
```

Every container then starts normally and none of them can resolve another by
name, which is the only way anything here reaches anything else: five units
on `sre-tab.network` connect to the database as `sre-tab-db`, and the
Caddyfile's upstream is `sre-tab-app:8000`. So the symptom is not a DNS error
but a psycopg one, out of `sre-tab-migrate.service`, reading like a database
that is down. Measured on a fresh Debian 13 host with podman 5.4.2, where
`deploy/scripts/smoke.sh` failed exactly that way at its first
cross-container step.

Both scripts now refuse rather than proceed: `install.sh --start` before it
enables the timers, and `smoke.sh` before it creates its network. A thinly
installed host is caught at install time rather than at the first request.
To ask a host directly:

```bash
podman info --format json | grep aardvark-dns
```

Three lines means it is there; nothing means it is not. Both scripts run
that same question, and neither looks for the binary on `PATH`, where it
never is — Debian installs it under `/usr/lib/podman`, other distributions
under `/usr/libexec/podman`, and `containers.conf` can move it. Asking for
the whole document and reading it, rather than for the one field that holds
the path, is deliberate: a `--format` template naming a field this podman
does not carry fails in exactly the way a missing binary does, and a check
that cannot tell those apart has to either refuse without evidence or
continue without checking. The document has no such state.

Then install:

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

<a id="the-published-port"></a>
### The published port is host policy

`sre-tab-web` publishes on `127.0.0.1:8080` by default, and 8080 is a
collision waiting to happen on any host that does more than one thing. On the
reference deployment it collided.

There was no supported way to say so, so the port was moved the only way
available: by editing `PublishPort=` in the tracked
`deploy/quadlet/sre-tab-web.container`. That works, and it is a local fork of
a file this repository owns. `git checkout`, `git stash`, and `git reset
--hard` each discard it without saying anything, and the next upstream change
to that file turns `git pull` into a conflict. Nor can the edit live in
`/etc`: `install.sh` copies every tracked Quadlet over
`/etc/containers/systemd` on every run, so an edited copy there survives until
the next install and no longer.

It is a setting instead. The file is `/etc/sre-tab/install.env`, which holds
host policy — things about *this host* that the repository cannot know — and
which the installer reads and never writes:

```bash
sudo install -m 0644 /etc/sre-tab/install.env.example /etc/sre-tab/install.env
sudo sed -i 's/^#SRE_TAB_WEB_PORT=.*/SRE_TAB_WEB_PORT=8081/' /etc/sre-tab/install.env
sudo deploy/install.sh --start
```

Its absence is the default, so a host that never creates it gets exactly what
it got before this file existed. That is not a claim about intent — the
installer writes nothing at all when the setting is absent, and removes the
drop-in described below if a previous run wrote one, so commenting the line
out and re-running returns the host byte for byte to a host that never had it.

**Only the host side moves.** The container still listens on 8080, and three
things depend on that: the `:8080` site block in `deploy/Caddyfile`, the
image's own healthcheck, and the right-hand number in `PublishPort=`. They
have to agree with each other and none of the three is an operator's
business. `SRE_TAB_WEB_PORT` sets the middle field of
`PublishPort=127.0.0.1:<host>:8080` and nothing else.

**It is not application configuration and does not belong in `app.env`.**
That file is handed to the running containers as an `EnvironmentFile=`, so
everything in it reaches a process environment and `podman inspect`. The port
is consumed by the installer, on the host, at install time, and the
application never reads it.

**The installer stages; only a restart adopts.** Changing the value and
running `deploy/install.sh` rewrites the generated drop-in and reloads the
generator; the running container goes on publishing the old port until
`sre-tab-web.service` restarts. `--start` does both. This is the same
stage-then-adopt split as [step 4 and step 5 of the roles
cutover](#cutting-a-running-deployment-over-to-the-roles), and for the same
reason.

**And tell the host's TLS proxy.** It is the one participant in this topology
that nothing in this repository can reach, and a moved port that the proxy
does not know about is a site that 502s.

#### What the installer writes, and the three mechanisms it is not

The port arrives as a **Quadlet drop-in**, at
`/etc/containers/systemd/sre-tab-web.container.d/10-published-port.conf`:

```ini
[Container]
PublishPort=
PublishPort=127.0.0.1:8081:8080
```

Quadlet merges `<unit>.container.d/*.conf` exactly as systemd merges a
`.service.d` drop-in. This installer already relies on that one layer up —
`sre-tab-backup.service` gets its `OnSuccess=` from a `.service.d` drop-in
rather than from an edit to `sre-tab-backup.container` — so the mechanism is
the deployment's existing answer to "change one line of a unit without editing
the unit". The tracked unit is installed byte for byte and the port arrives
beside it, which means `diff` between `deploy/quadlet` and
`/etc/containers/systemd` stays empty on a host that has moved its port. That
is precisely the property the local edit destroyed.

**The bare `PublishPort=` is the load-bearing line**, and it is the half of
this that a naive implementation gets wrong. Quadlet honours systemd's list
semantics: an assignment *adds*, and an empty assignment *resets*. Measured on
Debian 13 with podman 5.4.2, a drop-in without the reset generates

```
--publish 127.0.0.1:8080:8080 --publish 127.0.0.1:8081:8080
```

— the old port still open beside the new one. The new port works, so nothing
about the symptom points at the cause.

Three other mechanisms were tried and are not used. Each is the obvious idea
from a different direction, and the reason each was rejected is measured
rather than assumed:

- **A `.service.d` drop-in on the generated unit.** `PublishPort=` ends up
  inside the generated `ExecStart=`, so overriding it there means restating
  podman's entire command line — and re-deriving it after every podman
  upgrade, silently, because a stale copy still starts a container.
- **A variable in the unit file.** Quadlet does not expand one.
  `PublishPort=127.0.0.1:${SRE_TAB_WEB_PORT}:8080` is copied through to
  `--publish 127.0.0.1:${SRE_TAB_WEB_PORT}:8080` verbatim, with
  `podman-system-generator --dryrun` reporting no error at all.
- **That same variable, expanded by systemd.** This one *works*, which is why
  it is worth naming rather than dismissing. Adding `Environment=` and
  `EnvironmentFile=-` under `[Service]` makes systemd substitute the value
  into the generated `ExecStart` at start, mid-word substitution included: on
  a throwaway unit built exactly that way, the file absent published on the
  `Environment=` default, and the file naming 8091 published on 8091.

  It was rejected on the third case. An empty assignment expands to `--publish
  127.0.0.1::8080`, which podman accepts by publishing on **a random ephemeral
  port** — measured, `8080/tcp -> 127.0.0.1:37341`, with `systemctl start`
  exiting 0 and `systemctl --failed` empty. The unit is up, nothing is
  complaining, and the front door is somewhere nobody is looking. It also puts
  the value beyond validation: it is read at container start rather than at
  install time, so an operator who edits the file and never runs the installer
  gets whatever they typed. A mechanism whose failure is silent is worse than
  the hardcoded line it replaces.

#### What the installer refuses

The value is validated before anything at all is installed, so a bad one
leaves the host exactly as it was rather than half-written. It must be a whole
number from 1 to 65535 with no leading zero; empty, `0`, `70000`, `8080x`, and
`08080` are each refused with the offending value quoted back, and the
installer exits 2 having touched nothing.

Refusing here rather than later is the point, and the later is measured.
Quadlet generates a perfectly well-formed unit for `--publish
127.0.0.1:70000:8080` and `podman-system-generator --dryrun` exits 0 over it.
The refusal arrives at container start:

```
Error: parsing host port: port numbers must be between 1 and 65535 (inclusive), got 70000
qport.service: Main process exited, code=exited, status=125/n/a
```

Which is during a restart, with the old container already gone.

A port below 1024 is **accepted with a warning, not refused**. Rootful podman
can bind one and this unit publishes on loopback only, so there is no
measurement here that would support a refusal and inventing one would refuse a
choice that works. It is still worth a second look, because below 1024 is
where the host's own listeners are — including, on the documented topology,
the TLS proxy that forwards to this very port — and that collision does not
appear until the next restart.

#### Proving it, without moving this host's port

Staged into a temporary directory, so nothing under `/etc` is touched and no
unit is restarted. It needs no root for the same reason:

<!-- docs:run -->
```bash
stage=$(mktemp -d)
install -d "$stage/etc/sre-tab"
printf 'SRE_TAB_WEB_PORT=8099\n' > "$stage/etc/sre-tab/install.env"
DESTDIR="$stage" deploy/install.sh

QUADLET_UNIT_DIRS="$stage/etc/containers/systemd" \
    /usr/lib/systemd/system-generators/podman-system-generator --dryrun \
    > "$stage/dryrun"

published=$(grep -o -- '--publish [^ ]*' "$stage/dryrun")
echo "$published"
[ "$published" = '--publish 127.0.0.1:8099:8080' ]

rm -f "$stage/etc/sre-tab/install.env"
DESTDIR="$stage" deploy/install.sh
[ ! -e "$stage/etc/containers/systemd/sre-tab-web.container.d" ]
rm -rf "$stage"
```

One equality against the *whole* set of published ports, rather than one
`grep` for the new port and another for the absence of the old one. It says
both things at once — the new port arrived, and nothing else is published —
and it says them in a form that `set -e` actually acts on.

**That second clause is not stylistic, and it is here because the obvious
version of this block was written first and did not work.** It ended

```bash
grep -q -- '--publish 127.0.0.1:8099:8080' "$stage/dryrun"
! grep -q -- '--publish 127.0.0.1:8080:8080' "$stage/dryrun"
```

which reads correctly and is a guard that cannot fail. Deleting the reset line
from the installer on purpose produced an `ExecStart` carrying `--publish
127.0.0.1:8080:8080 --publish 127.0.0.1:8099:8080` — the exact breakage — and
the block still **exited 0**. POSIX says `set -e` is ignored for a pipeline
beginning with the `!` reserved word, so a `!`-prefixed assertion is a comment
with a trace line. Measured under bash 5.2 on Debian 13, in the harness that
runs this document.

One more green check that reported success while verifying nothing, in a
repository that has now shipped seventeen of them under other names — and it
was caught only by breaking the thing it guards rather than by reading it. The
rewritten form goes red on the same sabotage, and red again when the installer
is stopped from removing a stale drop-in. Writing the output to a file first
removes the question.

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

Eight values are secret to the running stack, and none of them appears in a
unit file, in `podman inspect`, or on a command line. Two scripts write them,
and which script writes which is worth knowing, because they are run at
different moments and for different reasons.

`deploy/scripts/create-secrets.sh` writes the superuser's four:

| Podman secret | Consumed as | By |
| --- | --- | --- |
| `sre-tab-postgres-password` | `POSTGRES_PASSWORD` | database (and `restore.sh`, for `DROP`/`CREATE DATABASE`) |
| `sre-tab-database-url` | — | **nothing, since the cutover.** Keep it: it is the rollback |
| `sre-tab-session-secret` | `SESSION_SECRET` | app |
| `sre-tab-github-client-secret` | `GITHUB_CLIENT_SECRET` | app |

`deploy/scripts/create-roles.sh` writes the three least-privilege roles' four:

| Podman secret | Consumed as | By |
| --- | --- | --- |
| `sre-tab-migrate-database-url` | `DATABASE_URL`; `SRE_TAB_RESTORE_URL` | `sre-tab-migrate.service`, and `restore.sh`'s `pg_restore` step |
| `sre-tab-app-database-url` | `DATABASE_URL` | `sre-tab.service` and `sre-tab-prune-sessions.service` |
| `sre-tab-readonly-password` | `PGPASSWORD` | `sre-tab-backup.service`, with `PGUSER=sretab_readonly` |
| `sre-tab-readonly-database-url` | `DATABASE_URL` | `sre-tab-status.service` |

**Four secrets for three roles**, because `sretab_readonly` has two consumers
that want the same password in different shapes: `pg_dump` takes a bare one
through `PGPASSWORD`, and `sre-tab status` takes a whole `DATABASE_URL` like
every other application unit. `create-roles.sh` writes both from one generated
password and `--rotate` moves both together — a rotation that moved one and
not the other would leave the backup and the hourly health check on different
passwords, with only one of them failing.

**Two secrets stopped being read by any unit at the cutover, and neither
should be deleted.** `sre-tab-database-url` carries the superuser's
`DATABASE_URL` and is now read by nothing at all; `sre-tab-postgres-password`
is still read by the database container as `POSTGRES_PASSWORD`, but no longer
by the backup. Both are the rollback path: reverting the cutover commit
points four units straight back at them, and a host that has quietly lost
either cannot take it. `install.sh --start` checks for all eight for exactly
this reason.

The roles are no longer optional. Every unit but the database connects as one
of them, so `create-roles.sh` is a required step on a first install — see the
ordering under [First start](#first-start), which is counter-intuitive because
the roles have to be created against a database that is already running.
[deploy/ROLES.md](ROLES.md) has the whole picture, and
[the runbook below](#cutting-a-running-deployment-over-to-the-roles) is how to
do it to a deployment that is already up.

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
`initdb`. Change it in the database first, then rotate the secrets:

```bash
podman exec -it sre-tab-db psql -U sretab -c "\password sretab"
sudo deploy/scripts/create-secrets.sh --rotate-db < /path/to/github-client-secret
```

**No unit needs restarting for this one, since the cutover.** The superuser's
password is read by `sre-tab-db.service` only at `initdb`, and by `restore.sh`
at the moment it runs; the application and the migration unit stopped reading
`sre-tab-database-url` when they moved to their own roles. That is the reverse
of the situation for the three role passwords, where a rotation *must* be
followed by a restart — a running container never picks up a changed podman
secret. `deploy/ROLES.md` has that procedure.

<a id="first-start"></a>
## First start

**Start the database on its own first, and install the roles against it.**
This step looks like it is in the wrong place and is not. Every unit but the
database connects as one of the three least-privilege roles, and
`create-roles.sh` creates those roles by talking to the *running* database —
so the roles cannot exist before the database does, and the rest of the stack
cannot start before the roles do. The database is the one unit that can be
started on its own, because it depends on nothing but the network:

<!-- docs:run -->
```bash
sudo systemctl start sre-tab-db.service
sudo deploy/scripts/create-roles.sh
```

That is once per host. `create-roles.sh` is idempotent and safe to re-run; it
leaves an existing role's password alone unless asked to rotate it.

Then the rest:

<!-- docs:run -->
```bash
sudo deploy/install.sh --start
```

`--start` refuses to proceed if any of the eight secrets is missing, and when
one of the four role secrets is the missing one it prints the three commands
above rather than only naming the secret. It enables every timer under
`deploy/systemd` — the backup and the session sweep — and restarts all five
long-running units in a single `systemctl` transaction, which is what makes
systemd resolve the ordering between them rather than starting them in the
order typed.

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

The port is asked of the front door rather than assumed, here and everywhere
else below. 8080 is only the default — see [The published port is host
policy](#the-published-port) — and a runbook that hardcodes it sends an
operator who has moved it to a port nothing serves, where a healthy
deployment is indistinguishable from a failed one. `${port:?…}` rather than a
fallback to 8080, so an unanswerable question fails saying so instead of
being answered with a guess:

<!-- docs:run -->
```bash
systemctl status --no-pager sre-tab-db.service sre-tab.service sre-tab-web.service

port=$(sudo podman port sre-tab-web 8080/tcp | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p' | head -1)
echo "front door: 127.0.0.1:${port:?sre-tab-web publishes no port, so it is not running}"

ready=false
for _ in $(seq 1 60); do
    if curl --fail --silent --max-time 5 --output /dev/null \
            "http://127.0.0.1:$port/api/v1/healthz"; then
        ready=true
        break
    fi
    sleep 2
done
if [ "$ready" != true ]; then
    echo "healthz did not answer within two minutes" >&2
    exit 1
fi

curl --fail --silent --max-time 5 "http://127.0.0.1:$port/api/v1/healthz"
curl --fail --silent --max-time 10 --output /tmp/sre-tab-index.html "http://127.0.0.1:$port/"
head -5 /tmp/sre-tab-index.html
```

Quadlet services are transient generated units and cannot be enabled with
`systemctl enable`; their `[Install]` sections are applied by the generator at
boot and on `daemon-reload`, so starting them explicitly is enough. The timers
are native units and are enabled normally.

<a id="cutting-a-running-deployment-over-to-the-roles"></a>
## Cutting a running deployment over to the roles

For a host that is already up and still connecting as the superuser. A fresh
install does not need this section — [First start](#first-start) already has
the roles in it.

Read this much before starting, because it is the part that decides whether
tonight is the night:

- **It needs no new image.** The cutover changes which credential each unit is
  handed and nothing else; no code in `app/` is involved, and the digest
  currently pinned works exactly as it stands. Do **not** fold a `promote.sh`
  into this. One change at a time is the whole reason the cutover is a single
  commit with a one-command rollback, and a promotion in the same window
  entangles the two — if something misbehaves you want to know which of them
  did it.
- **Budget one application restart, and it is a small one.** Measured on the
  reference host across step 5, polling five times a second: the API answered
  `502` for **6.4 seconds** and the SPA document never stopped answering `200`
  at all. That is much shorter than a promotion's window, and for a reason
  worth knowing rather than trusting — this restarts two units and neither is
  Caddy, so the published port is never withdrawn and the netavark hostport
  tail described under
  [How long a deploy actually takes](#how-long-a-deploy-actually-takes) does
  not happen. A user with the page open sees failing API calls, not a dead
  site.

  One thing from that section does still apply, in the opposite direction:
  `systemctl` returned at **16.3s**, roughly ten seconds *after* the service
  was answering again, because `Notify=healthy` waits for the image's
  healthcheck. Do not read the prompt coming back as the moment service
  resumed; it is later than that, not earlier.
- **The reversible point is step 4.** Everything before it can be abandoned by
  doing nothing at all.
- Every step below has been executed end to end on a Debian 13 host with
  podman 5.4.2, rollback included.

### 1. Take a backup, and check it arrived

`create-roles.sh` reassigns the owner of every table in `public`. That is a
change to the database, not only to unit files, so this is the one step not to
skip.

```bash
sudo systemctl start sre-tab-backup.service
sudo journalctl -u sre-tab-backup.service -n 5 --no-pager
sudo ls -l /srv/sre-tab/backups | tail -3
```

**Good:** a `backup complete: … (N bytes)` line, and a dump with today's
timestamp beside a `.sha256` sidecar. This one still runs as the superuser;
that is expected, it is the last such run.

### 2. Pull the commit that carries the cutover

```bash
cd /path/to/sre-tab && git pull
git log --oneline -1
grep -h '^Secret=.*DATABASE_URL' deploy/quadlet/*.container
```

**Good:** four `Secret=` lines — `sre-tab-app-database-url` twice,
`sre-tab-migrate-database-url` once, and `sre-tab-readonly-database-url` once
— and no `sre-tab-database-url` anywhere. If you see `sre-tab-database-url`,
you are not on the right commit; stop.

### 3. Install the roles

The database is already running, so this is one command.

```bash
sudo deploy/scripts/create-roles.sh
```

**Good:** `sretab_migrate: role created` and the same for `sretab_app` and
`sretab_readonly`, four `wrote secret` lines, then a `NOTICE: reassigned
public.<table> to sretab_migrate` line per existing table, then `Done.`
Check:

```bash
sudo podman secret ls --format '{{.Name}}' | grep sre-tab- | sort
sudo podman exec sre-tab-db psql -U sretab -d sretab -c \
  "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname LIKE 'sretab%' ORDER BY 1"
```

**Good:** eight secrets, and `f` in all three columns for the three
`sretab_*` roles — only `sretab` itself is `t`.

If it refuses because a role and its secret disagree about whether they exist,
it will say which way round, and `--rotate` is the answer. `deploy/ROLES.md`
has the reasoning; nothing is broken and nothing has changed yet.

**Nothing that is running has changed.** The application is still connected as
the superuser. Walking away here costs nothing.

### 4. Stage the new units

```bash
sudo deploy/install.sh
grep -h '^Secret=.*DATABASE_URL\|^Environment=PGUSER' \
  /etc/containers/systemd/sre-tab*.container
```

**Good:** the same three `Secret=` lines as in step 2, plus
`Environment=PGUSER=sretab_readonly`.

Still nothing running has changed: a staged unit file has no effect on a
container that is already up. This is the last cheap stopping point.

### 5. Restart the two units that hold a credential

```bash
sudo systemctl restart sre-tab-migrate.service sre-tab.service
```

One invocation, not two, so systemd builds a single transaction and honours
the ordering between them.

**Why a restart is needed at all:** a running container reads a podman secret
once, at start, and holds what it read. Changing the secret — or changing
which secret the unit names — does nothing to the process that is running.
`install.sh` stages; only a restart adopts.

**Why these two and not five.** `sre-tab-prune-sessions.service`,
`sre-tab-backup.service`, and `sre-tab-status.service` also changed, but all
three are timer-driven oneshots that are not running, so there is nothing to
restart: each picks up the new unit file and the new secret by itself at its
next elapse. `sre-tab-web.service`
and `sre-tab-assets.service` never touch the database. So the units that must
move *together* are the migration unit and the application, because the
application `Requires=` the migration unit and must not come up against a
schema a failed migration left behind.

### 6. Check it, and do not trust the prompt returning

```bash
systemctl --failed --no-pager

port=$(sudo podman port sre-tab-web 8080/tcp | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p' | head -1)
ready=false
for _ in $(seq 1 60); do
    if curl --fail --silent --max-time 5 \
            "http://127.0.0.1:${port:?}/api/v1/healthz"; then
        ready=true
        break
    fi
    sleep 2
done
echo
[ "$ready" = true ] || echo "NOT READY after two minutes - do not proceed, see rollback below"
```

Caddy is not restarted by this cutover, so it is up and can be asked which
port it publishes. `${port:?}` rather than 8080, for the reason given under
[First start](#first-start).

The flag is the point of that loop, not decoration. Without it the loop simply
stops after two minutes and the next command runs, so an application that never
became ready reads exactly like one that did — which is the failure this
section's heading is about, arriving inside the check written to prevent it.

**Good:** no failed units, and `{"status":"ok","live":true,"ready":true,…}`
with the database probe `"ok":true`.

Then the question this whole exercise is about — *which role is actually
connected?* Ask the database, not the unit file:

```bash
sudo podman exec sre-tab-db psql -U sretab -d sretab -c \
  "SELECT usename, count(*) FROM pg_stat_activity
    WHERE datname = 'sretab' AND pid <> pg_backend_pid()
    GROUP BY usename ORDER BY 1"
```

**Good:** `sretab_app` and nothing else. Seeing `sretab` means a container is
still running on the old credential — almost always because step 5 was run
before step 4, so `install.sh` had not yet staged the new unit.

`pid <> pg_backend_pid()` is not tidiness: this `psql` connects as the
superuser, so without it the query counts itself and always reports one
`sretab` connection. That is a false alarm on the one check the whole
procedure turns on, and it is in here because the first draft of this runbook
had it and the run caught it.

And the migration unit, which has already run by now:

```bash
sudo journalctl -u sre-tab-migrate.service -n 20 --no-pager
```

**Good:** alembic reporting a context and either running upgrades or finding
none to run, and the unit `active (exited)` with status 0.

### 7. Exercise the three timer-driven units now, not at 03:22

They are the units nobody watches, and a credential problem in any of them is
silent until the backup directory has a fortnight-shaped hole in it.

```bash
sudo systemctl start sre-tab-backup.service
sudo journalctl -u sre-tab-backup.service -n 5 --no-pager

sudo systemctl start sre-tab-prune-sessions.service
sudo journalctl -u sre-tab-prune-sessions.service -n 5 --no-pager

sudo systemctl start sre-tab-status.service
sudo journalctl -u sre-tab-status.service -n 20 --no-pager
```

**Good:** another `backup complete: … (N bytes)` — this one taken as
`sretab_readonly` — either `deleted N dead session rows` or
`no dead sessions`, and the status table with a row per source. A `permission
denied` in any of them is the cutover having gone wrong for that unit
specifically; roll back.

`sre-tab-status.service` is the one of the three whose *own* failure is not a
credential problem: it exits non-zero when a source has genuinely been failing,
which is what it is for, and `systemctl start` on it therefore fires the alert
path as well. Read the journal rather than the exit status — a
`permission denied for table sources` is the cutover; a source named with an
error class beside it is the check working.

Compare the byte count with the dump from step 1. They should be in the same
ballpark. A dump that is suddenly tiny means `pg_dump` read less than it used
to, which is what a missing grant looks like when it does not fail outright.

### If it is not good: roll back

Three commands, and they have been run:

```bash
git revert --no-edit <the cutover commit>
sudo deploy/install.sh
sudo systemctl restart sre-tab.service sre-tab-migrate.service \
  sre-tab-prune-sessions.service sre-tab-status.service \
  sre-tab-backup.service
```

The last three are oneshots, so restarting them runs them — which is the
point, because it re-proves them on the superuser credential rather than
leaving them staged and unexercised until the small hours. Expect
`sre-tab-status.service` to exit non-zero if a source is genuinely failing;
that is the check working, not the rollback failing.

This works because nothing above ever touched `sre-tab-database-url` or
`sre-tab-postgres-password`. **Do not delete either**, then or later; leaving
them in place *is* the rollback. Leave the three roles and their four secrets
in place too — nothing references them once the units are reverted, and the
next attempt reuses them as they are.

`deploy/ROLES.md` has the full reasoning, the verification record, and the
rotation procedure for the three role passwords.

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

The registry now also carries version tags — `1.1.0` for an exact release,
`1.1` for the newest patch on that line — and none of that changes anything
here. They exist for people running this image outside these Quadlets, and
[README.md](../README.md#installing-a-version) is written for them. `1.1` is
a moving pointer with the same property `:latest` had, which is why it is not
what these units name. A release is promoted by digest like any other build:
`deploy/scripts/promote.sh sha-<commit>`, using the commit the tag points at.

<a id="promote-a-build"></a>
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

Three units are deliberately absent from that list, for opposite reasons.
`sre-tab-web.service` is Caddy and does not run the application image, so a
promotion never changes it. `sre-tab-prune-sessions.service` and
`sre-tab-status.service` do run it, but both are timer-driven and neither is
running, so there is nothing to restart — each adopts the new digest by
itself at its next elapse, once `install.sh` has staged the rewritten unit.
So the five units a promotion rewrites and the four services this command
restarts overlap in three, and neither set contains the other.

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
`sre-tab-web.container` publishes on loopback deliberately, because TLS is
terminated by the host's existing proxy, and that proxy reaches Caddy over
loopback exactly as the polling did. There is no off-host client of this port
to compare against, so nothing insulates users from the tail: it is what the
outer proxy sees, and it reaches them as 502s for the duration.

The measurements in this section were taken on 8080, which was the only port
this unit could publish on at the time. Nothing here depends on the number —
the mechanism is netavark's hostport rule and podman's reservation listener,
and both are per-port — but the figures are that host's, on that port, and
have not been re-measured since the port became a setting.

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
| Publish (CI) | every push to main, and every version tag | a signature or attestation that cannot be verified from outside the step that made it |
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

Caddy is published on `127.0.0.1` only, on port 8080 unless the host says
otherwise. Terminate TLS at the host's existing proxy or load balancer and
forward to that address.

**Ask the host rather than assuming the number.** `SRE_TAB_WEB_PORT` in
`/etc/sre-tab/install.env` moves the published port — see [The published port
is host policy](#the-published-port) — and this proxy is the one participant
in the topology that nothing in this repository configures, so a moved port
that it does not know about is a site that 502s with no failed unit anywhere
to explain it. What the front door is actually on:

```bash
sudo podman port sre-tab-web 8080/tcp
```

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
  loopback over HTTP is the design and is expected; what must not happen is a
  browser reaching the site over HTTP.
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
   which is the source address a connection to the published loopback port
   arrives from once Podman has DNATed it. That address is a property of the
   network rather than of the port, so moving the published port with
   `SRE_TAB_WEB_PORT` does not change it and this block needs no edit.
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

It runs as `sretab_readonly` — `PGUSER` in the unit, `PGPASSWORD` from
`sre-tab-readonly-password` — so the job that reads every row in the database
every night cannot write one. That role holds `SELECT` on sequences as well as
on tables, which is not decoration: a custom-format dump emits a `setval()`
per sequence, and a role without it makes `pg_dump` fail outright rather than
produce a dump whose restore hands out ids that are already taken.

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
4. `DROP DATABASE ... WITH (FORCE)` and recreates it, as the superuser —
   database-level administration that none of the three least-privilege roles
   holds, and none of them should;
5. re-applies `deploy/roles.sql` to the new database when those roles exist,
   because grants and default privileges live *inside* a database and the
   drop took them with it;
6. restores as `sretab_migrate` — not as the superuser — with
   `--single-transaction --exit-on-error`, so the database ends up either
   fully restored or empty for a second attempt, never half-restored with the
   application then migrating on top of it;
7. re-applies `roles.sql` once more, which settles ownership and grants over
   whatever the restore actually created;
8. reports the table count and the `alembic_version` row;
9. restarts the application and waits for `/api/v1/healthz`.

Steps 4 and 6 are two different credentials on purpose, and
[deploy/ROLES.md](ROLES.md) carries the reasoning. On a host where the roles
were never installed, `--restore-user sretab` collapses them back into one;
the script checks for the role and says so before it drops anything, rather
than failing at authentication time with the database already gone.

Point-in-time recovery is out of scope for v1: these are nightly logical
dumps, so the recovery point is the last successful backup.

### The restore has been tested

`deploy/scripts/smoke.sh` runs the same `restore.sh`, unmodified, against a
throwaway database: it brings up PostgreSQL on an empty volume, installs the
three least-privilege roles, migrates as `sretab_migrate`, starts the
application and Caddy as `sretab_app`, checks the health endpoint and the
front-door behaviour, writes a marker row, takes a backup as
`sretab_readonly` with the real `backup.sh`, drops the marker, restores with
the real `restore.sh` and its split credential, and asserts that the marker
and the Alembic revision both come back, that the restored tables are owned
by `sretab_migrate` and readable by `sretab_app`, and that the application
goes healthy again.

```bash
CONTAINER_ENGINE=docker SRE_TAB_IMAGE=sre-tab:dev deploy/scripts/smoke.sh
```

CI runs it on every push under Podman. It has been run under Docker on macOS
during development and passes end to end.

Since the cutover it also opens the four unit files before it starts anything
and refuses to run if they name credentials other than the ones it is about to
use. Without that it was a test of the roles and not of the deployment: it
invents its own connection strings — it has no podman secrets and under
`CONTAINER_ENGINE=docker` cannot have any — so every assertion in it would
have gone on passing with all four units reverted to the superuser.

What the smoke test still does **not** cover, because systemd is not involved
in it: Quadlet generation into live units, the `After=` and `Requires=`
ordering holding at boot, `Notify=healthy`, and the Podman secret plumbing. CI
validates unit *generation* with `podman-system-generator --dryrun`, which
catches malformed keys and nothing else. Those have now been exercised by hand
on three separate Debian 13 hosts, most recently on the cut-over units from a
completely empty host — see the verification record in
[deploy/ROLES.md](ROLES.md#the-cutover-itself-was-run). What remains genuinely
unproven is the timers *firing on their own*, and `Persistent=true` catching
up after downtime; starting the units by hand, which the runbook above does,
tests the job and not the schedule.

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
one of the five `deploy/scripts/promote.sh` moves together and CI checks for
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

<a id="alerting-on-a-failing-source"></a>
## Alerting on a failing source

`sre-tab-status.timer` runs `sre-tab status --failures-over 3` in the
application image every hour at :48 with up to five minutes of jitter. When it
exits non-zero, `OnFailure=` on `sre-tab-status.service` starts
`sre-tab-alert@sre-tab-status.service.service`, which gathers the failed
unit's journal and hands it to a transport the operator writes.

> **On first install of this release, promote before you wait for the timer.**
> `sre-tab-status.container` pins a digest the way every application unit
> here does, and on the release that introduces this feature that pin is
> necessarily older than the `--failures-over` flag the unit passes. Until
> `deploy/scripts/promote.sh` moves all six units onto a build that has the
> flag, the hourly check starts, resolves its secret, and exits 2 with
> `unrecognized arguments: --failures-over 3`. That does alert — correctly,
> in the sense that a unit really is failing — but about the wrong thing, and
> it will do so every hour until the digest moves. Promote first and the
> question never arises. This is the ordinary pin-then-promote sequence and
> not a fault in the unit; see [Promote a build](#promote-a-build).

It connects as `sretab_readonly`, from `sre-tab-readonly-database-url` — the
only application unit that holds no write access at all, because the check is
two `SELECT`s and never commits. It shares the role with the nightly backup
and shares the image with the session sweep, and is deliberately not on the
sweep's credential: an unattended hourly job is the last thing that should be
able to write. [deploy/ROLES.md](ROLES.md) has the reasoning and the gates
that keep the two apart.

The reason it exists: **`/api/v1/healthz` knows a source is failing and
deliberately will not say so.** `app/scheduler/service.py` reports `ok=true`
with the failure count in its detail string, because one broken feed must not
take the instance out of rotation. Readiness and alerting want opposite
answers to the same question, and until this timer existed only one of them
was being asked — a source could stop fetching indefinitely and the only
symptom would be stale items nobody was looking for.

### Wire up a transport, or the alert reaches nobody

**This is the one step that is not automatic.** `install.sh` never writes
`/etc/sre-tab/alert.sh`, because reaching a person is a property of the host
— mail, a webhook, a pager, an agent that is already installed — and picking
one here would put a transport dependency in a project that has deliberately
few. It installs `alert.sh.example` beside it instead, with two worked
implementations:

```bash
sudo cp /etc/sre-tab/alert.sh.example /etc/sre-tab/alert.sh
sudo $EDITOR /etc/sre-tab/alert.sh     # msmtp, or curl to a webhook
sudo chmod 0755 /etc/sre-tab/alert.sh
```

The mail example posts through `msmtp` with an explicit envelope sender; the
webhook example reads its URL from a mode-0600 file — the URL is a credential
— and builds the JSON with `jq`, because the report contains newlines, quotes,
and whatever a feed's error detail happened to say.

**Without that file the alert is not silent, and that is on purpose.**
`install.sh` warns at the end of every run while it is missing, the whole
report still reaches this host's journal under the alert unit, and
`alert-dispatch.sh` exits 1 so the alert unit lands in `systemctl --failed`
naming the file it wanted. An alerting path that fails quietly is the exact
defect this pair of units was written to remove, so its own misconfiguration
was not allowed to be the one thing that fails quietly.

Your script's exit status is the alert's exit status. Exit non-zero when the
message did not go out, and a dead relay shows up in `systemctl --failed`
rather than being believed. Do not add `|| true`.

### What the transport is handed

| | |
| --- | --- |
| `$1` | the failed unit, `sre-tab-status.service` |
| stdin | the whole report: unit, systemd's `Result`, exit status, timestamps, and the unit's last 50 journal lines — which for `sre-tab-status.service` is the status table and the per-source error lines |
| `$SRE_TAB_ALERT_UNIT` | the same as `$1` |
| `$SRE_TAB_ALERT_RESULT` | systemd's `Result`, e.g. `exit-code`, `timeout` |
| `$SRE_TAB_ALERT_STATUS` | the exit status, e.g. `1` |
| `$SRE_TAB_ALERT_HOST` | this host's name |

The journal is the alert body rather than a pointer to it, which is why
`sre-tab-status.container` sets `LogDriver=none`: systemd's own capture of the
container's stdout is then the single copy, and `journalctl -u` finds it.

### `--failures-over 3` means over three, not three

`sre-tab status` on its own still exits 1 for **any** enabled source with any
consecutive failure — that has not changed, and it is right for somebody
typing it. On an hourly timer it would page a human for one transient 502
from one feed, and an alert that fires on noise is an alert somebody mutes.

So the timer passes a threshold, and the threshold is strict:

| Consecutive failures | `--failures-over 3` |
| --- | --- |
| 1, 2, 3 | reported in the output, exit 0, no alert |
| 4 or more | reported, exit 1, alert |

At the default 30-minute refresh interval each failure is another half hour
with no successful fetch, so the fourth is roughly two hours of a source being
down. Pass `--failures-over 2` to page on the third instead. The failing
source is printed at every threshold, so the report the alert carries is the
same either way; only the exit code moves.

### A malformed slug alerts every hour until it is fixed

`sre-tab status` also exits 1 when a source or topic slug predates the format
check, and **`--failures-over` does not gate that half.** This is deliberate
and it is the one behaviour here worth knowing before 03:00.

The threshold counts consecutive *fetch* failures. A malformed slug never
increments that counter — the source fetches perfectly and simply cannot be
filtered to — so gating it behind the threshold would mean any value above
zero suppressed a permanent configuration defect for ever, which is strictly
worse than the noise. And unlike a fetch failure it never self-heals, so the
alert repeats hourly until somebody acts:

```bash
podman exec sre-tab-app sre-tab status      # names the offending slug
```

Fix it by re-adding the source or topic under a valid slug; the existing row
cannot be renamed in place without breaking every saved selection that names
it. If your transport pages rather than files a ticket, put deduplication in
whatever receives the alert — that is the piece that knows what a duplicate
means to you.

### Testing the alert path by hand

A green check is not a passed check, and an alert path that has never fired
is not an alert path. Fire it:

```bash
# 1. The transport alone, with a synthetic report.
sudo systemctl start sre-tab-alert@sre-tab-status.service.service
sudo journalctl -u 'sre-tab-alert@sre-tab-status.service.service' -n 50

# 2. The whole chain — a unit that fails, an OnFailure=, an alert:
sudo systemd-run --unit alert-probe \
    --property=OnFailure=sre-tab-alert@alert-probe.service.service /bin/false
sudo journalctl -u 'sre-tab-alert@alert-probe.service.service' -n 50
sudo systemctl reset-failed alert-probe.service
```

The first proves the transport and the report. The second proves the
`OnFailure=` wiring end to end, including that the failed unit's name reaches
the template — the alert's journal names `alert-probe.service` throughout and
quotes its last 50 lines.

The instance name is written out in full there rather than as `%n`, and that
is not a style choice: `systemd-run` does **not** expand specifiers inside
`--property=`, and refuses the unit outright with `Invalid unit name
sre-tab-alert@%n.service`. In `sre-tab-status.container`'s `OnFailure=` line
the specifier is expanded normally, which is why that one is `%n` — `%n` is
the full unit name, so the instance becomes `sre-tab-status.service` and
`journalctl -u %i` inside the template needs no suffix appended. `%N` would
drop the `.service` and leave the template re-deriving it.

To confirm the threshold rather than assume it, set a source's counter
directly and watch the exit code move between 3 and 4:

```bash
seed() {
  podman exec sre-tab-db psql -U sretab -d sretab -c \
    "INSERT INTO source_status (source_id, last_fetched_at, consecutive_failures)
     SELECT id, now(), $1 FROM sources WHERE slug = 'lwn'
     ON CONFLICT (source_id) DO UPDATE
        SET consecutive_failures = EXCLUDED.consecutive_failures;"
}

seed 3 && sudo systemctl start sre-tab-status.service   # succeeds, no alert
seed 4 && sudo systemctl start sre-tab-status.service   # fails, and alerts
```

An `INSERT ... ON CONFLICT` rather than an `UPDATE` because a source that has
never fetched has no `source_status` row at all, and an `UPDATE` against it
reports `UPDATE 0` and changes nothing — which then reads exactly like a
threshold that is not working. The scheduler overwrites `consecutive_failures`
on the source's next refresh, so this leaves nothing behind.

### What this alert does not cover

`OnFailure=` fires when a unit enters a failed state. It does **not** fire
when the start job is cancelled because `Requires=sre-tab-db.service` failed:
that path leaves `sre-tab-status.service` inactive rather than failed. A
database that is down is therefore reported by `sre-tab-db.service`'s own
failure and by `systemctl --failed`, not by this alert. A database that is up
but unreachable — a wrong `DATABASE_URL`, a removed network — does fire it,
because the check runs and fails.

The timer is also **not** `Persistent=true`, unlike the backup and the session
sweep. Those catch up because a missed run is work that did not happen; this
is a question whose answer is about now, and the next run is at most an hour
away. A catch-up run would fire seconds after boot, against counters that are
whatever they were before the host went down, at the moment an operator is
already dealing with a host that has just come back.

## Operations

```bash
systemctl list-timers 'sre-tab-*'
systemctl status sre-tab-db.service sre-tab.service sre-tab-web.service
journalctl -u sre-tab.service --since today
journalctl -u sre-tab-migrate.service -n 50
podman logs sre-tab-web

# 8080 is the default; SRE_TAB_WEB_PORT may have moved it, and the front door
# is the thing that knows. See "The published port is host policy".
port=$(podman port sre-tab-web 8080/tcp | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p' | head -1)
curl --fail "http://127.0.0.1:${port:?}/api/v1/healthz" | jq
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

A failing source no longer waits for somebody to run the CLI: `sre-tab-status.timer`
asks hourly and `OnFailure=` carries the answer to a person. See
[Alerting on a failing source](#alerting-on-a-failing-source), and note that
it needs one file written by hand before it can reach anybody.

`systemctl --failed` is only worth watching if it is empty when nothing is
wrong, so a clean `systemctl stop` has to leave it clean. That is what the
`NoNewPrivileges` note below is protecting — and it matters more now that
`systemctl --failed` is where an unconfigured alert path lands.

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
