# Database roles

`deploy/roles.sql` and `deploy/scripts/create-roles.sh` close the one
finding in `ROADMAP.md` whose severity the operator count does not cap: the
application, the migration unit, and the backup all connect to PostgreSQL
as `POSTGRES_USER=sretab`, which the official postgres image creates as the
cluster superuser. An application-level SQL injection therefore does not
stop at the tables the ORM reaches — `COPY ... PROGRAM` is available to a
superuser, and it executes commands.

This document covers what the three roles are, how to install them, and —
because installing them and using them were two different, deliberately
separated decisions — the cutover that switched the deployment over to them.

**The cutover has happened.** Every unit under `deploy/quadlet/` now connects
as one of the three, and no unit but the database container itself carries a
superuser credential. The application cannot `CREATE TABLE` and cannot
`COPY ... TO PROGRAM` — demonstrated from inside the running application
container on a real Podman host, not only from a test harness, and recorded
under "[Verification](#verification)" below.

It landed in that order on purpose. `restore.sh` took a split credential and
`smoke.sh` learned to run its whole throwaway stack as the three roles
*first*, so that the commit which finally changed the `Secret=` lines was the
smallest thing it could be and was proved rather than believed. The operator
half of that ordering is still what matters day to day: the rollback is one
`git revert` because the cutover is one commit touching nothing but
`deploy/quadlet/`.

If you are here to perform the cutover on a host that is still running as the
superuser, the ordered procedure is in
[deploy/README.md](README.md#cutting-a-running-deployment-over-to-the-roles).
This document is why; that one is how.

## The three roles

| Role | Stands in for | May | May not |
| --- | --- | --- | --- |
| `sretab_migrate` | the migration unit (`alembic upgrade head`) | `CREATE`/`USAGE` on schema `public`; owns every table and sequence in it, so it can `ALTER` and `DROP` them too | superuser, `CREATEDB`, `CREATEROLE`, `COPY ... PROGRAM` |
| `sretab_app` | the application, and `sre-tab sessions prune` | `SELECT`/`INSERT`/`UPDATE`/`DELETE` on every table; `USAGE` and `SELECT` on every sequence | any DDL, `TRUNCATE`, `COPY ... PROGRAM` |
| `sretab_readonly` | `pg_dump` (the backup unit) | `SELECT` on every table and every sequence | any write, any DDL, `COPY ... PROGRAM` |

None of the three is superuser, and none can create a database or another
role. `COPY ... PROGRAM` — the specific mechanism the finding is about — is
refused for all three without any explicit `REVOKE`: PostgreSQL gates it
behind membership in the predefined `pg_execute_server_program` role, which
nothing here grants, so a plain non-superuser role is refused by default.
This was checked against a real server rather than assumed — see
"Verification" below.

### Why `sretab_app` gets `SELECT` on sequences, not just `USAGE`

`USAGE` is what makes a `SERIAL` primary key's implicit `nextval()` work on
`INSERT`, which is all the application strictly needs. `SELECT` is added
alongside it so the application can read a sequence's current value back
without needing a fourth privilege category introduced later for one
narrow case — it is not reachable through anything currently in `app/`, but
it costs nothing to have and saves a return trip to this file the day it
is.

### Why table-level `ALTER`/`DROP` needs ownership, not a grant

PostgreSQL has no `GRANT`-able "ALTER" or "DROP" privilege on a table —
only the owner (or a superuser) can do either. That is why
`sretab_migrate` is made the *owner* of every table and sequence rather
than merely granted broad privileges on them: ownership is the only
mechanism that lets it run a future migration that adds a column or drops
one, not only `CREATE TABLE` for new ones.

## Ownership

Schema `public` is left owned by whatever owns it today — the cluster
bootstrap superuser, by way of the `pg_database_owner` pseudo-role (checked
against a real postgres:18: `pg_namespace.nspowner` for `public` resolves
to `pg_database_owner`, which tracks whoever owns the database, not a
fixed role). `sretab_migrate` is granted `CREATE` and `USAGE` on the schema
rather than made its owner. The database is still fully controlled by the
superuser at the point `create-roles.sh` runs, so there is no reason to
reassign the schema itself and every reason to keep this script's
footprint to what the finding actually requires.

Every table in `public` is reassigned to `sretab_migrate`: the ones that
already exist (a one-off sweep in `roles.sql`, idempotent — it only
touches tables it does not already own) and every one a future
`alembic upgrade` creates, automatically, because `sretab_migrate` is the
role the migration unit runs `CREATE TABLE` as.

Sequences are **not** swept explicitly. Every sequence in this schema
backs a `SERIAL` primary key (`users`, `sessions`, `sources`, `topics`,
`feed_items` — the five tables with a bare `mapped_column(primary_key=True)`
integer column in `app/db/models.py`; every other table uses a composite
key and has no sequence), and PostgreSQL links such a sequence to its
column with an internal dependency that makes it follow the table's owner
automatically. This is not an assumption: reassigning `sources` to
`sretab_migrate` moved `sources_id_seq` with it in the same statement, and
attempting `ALTER SEQUENCE sources_id_seq OWNER TO ...` directly was
refused outright — `ERROR: cannot change owner of sequence "sources_id_seq"
... Sequence "sources_id_seq" is linked to table "sources"`. A future
migration that adds a genuinely standalone sequence (one that is not
`SERIAL`/`IDENTITY` on a column) would need its own explicit
`ALTER SEQUENCE ... OWNER TO sretab_migrate` — there is no such sequence
today.

`ALTER DEFAULT PRIVILEGES` is what covers tables and sequences that do not
exist yet: table-level grants only ever apply to objects that already
exist when the `GRANT` runs. The load-bearing detail is
`FOR ROLE sretab_migrate` — default privileges attach to the role that
*creates* an object, not to whoever runs the `ALTER DEFAULT PRIVILEGES`
statement itself (the superuser, when `create-roles.sh` is run). Naming
the wrong role there, or omitting `FOR ROLE` and taking the default of
"whoever runs this," would compile and apply without error and then simply
never fire, because the superuser is no longer the role that creates a table
here — the migration unit connects as `sretab_migrate`, and only a restore
run with `--restore-user sretab` ever creates one as anything else.

## Installing

```bash
sudo deploy/scripts/create-roles.sh
```

This creates the three roles (if they do not already exist), applies every
grant in `roles.sql`, and writes the three podman secrets the units consume:

| Podman secret | Holds | Read by |
| --- | --- | --- |
| `sre-tab-migrate-database-url` | a `DATABASE_URL` for `sretab_migrate` | `sre-tab-migrate.container`, and `restore.sh`'s `pg_restore` step |
| `sre-tab-app-database-url` | a `DATABASE_URL` for `sretab_app` | `sre-tab.container` and `sre-tab-prune-sessions.container` |
| `sre-tab-readonly-password` | just the password, for `PGPASSWORD` | `sre-tab-backup.container`, alongside `Environment=PGUSER=sretab_readonly` |

**It has to run against a database that is already up, which makes the
first-install ordering counter-intuitive.** `create-roles.sh` reaches the
cluster by `podman exec` into the running container, so the roles cannot
exist before the database does — and the database is started by
`install.sh --start`, which now refuses to run until the three secrets above
exist. A fresh host therefore starts the database on its own, in between:

```bash
sudo deploy/install.sh                     # stage the units
sudo deploy/scripts/create-secrets.sh < …  # the superuser's four secrets
sudo systemctl start sre-tab-db.service    # the database alone
sudo deploy/scripts/create-roles.sh        # the roles and their three secrets
sudo deploy/install.sh --start             # the rest of the stack
```

`install.sh --start` prints exactly that sequence when a role secret is
missing, rather than enabling the timers and then watching three units fail
to resolve a `Secret=` reference. That check was watched failing on purpose
before it was believed, on the reference host, and it exits before any timer
is enabled.

It runs the SQL through the postgres image's own `psql`, via
`podman exec` into the running database container over the unix socket —
not over `sre-tab.network` — so it needs no password for the *connecting*
superuser: the official image's default `pg_hba.conf` trusts local-socket
connections unconditionally (checked directly; only `host` lines require
`scram-sha-256`, and `podman exec` never goes through one). The three
generated role passwords never appear on a command line or in `podman
inspect` — they travel from the script to `psql` entirely over stdin, as
`\set` variables ahead of `roles.sql`'s own text, the same discipline
`create-secrets.sh` uses for the GitHub OAuth client secret.

**Running this script still changes nothing that is already running.** It
writes secrets and grants; it does not restart anything, and a container
holds the secret it was started with until it is restarted. On a host that
has already been cut over, that is the property the rotation note below
depends on. On a host that has not, it is what made installing the roles and
using them two separable decisions in the first place — the script is
deliberately not called from `install.sh`, and that has not changed.

Re-running it is safe. With no flags, a role or secret that already exists
is left exactly as it is — no password is rotated, nothing is overwritten
— while every grant is re-applied regardless, which is harmless (`GRANT`
and `ALTER DEFAULT PRIVILEGES` are no-ops when already in place) and is
what makes it safe to run again after a schema change, or after a change
to this tooling itself. `--rotate` regenerates all three passwords and
secrets together; see "Rotating a role's password" below, which now has real
consequences, because all three secrets are in use.

If a role and its podman secret disagree about whether they exist, the
script refuses to guess and asks for `--rotate` explicitly, in either
direction:

- **Secret exists, role does not** — should only happen from a manual
  `DROP ROLE`. Generating a role with an unknown password would never
  match the secret again.
- **Role exists, secret does not** — a manually deleted secret, or a
  partial host recovery. Writing a fresh secret without also rotating the
  role's password would still leave the two disagreeing, since the role's
  existing password is not known to this script; left unguarded, this case
  used to fall through silently and report success with no secret written,
  surfacing only later as a unit that will not start because its `Secret=`
  reference does not resolve — which, now that three units depend on these
  secrets, is an outage rather than a curiosity.

<a id="verification"></a>
## Verification

**Most of what follows is now asserted on every push** rather than only
recorded here: `deploy/scripts/smoke.sh` installs the roles, runs the whole
stack as them, and matches on the text of each refusal — see
"[`smoke.sh` tests the cutover](#smoke-tests-the-cutover)" below for the list.
A document is the wrong place for a claim a gate can hold, and this section
had exactly the shape that goes stale first.

Checked against a real `postgres:18-trixie` container (not assumed), after
running this repository's own Alembic migrations against it:

- `sretab_app` can `INSERT`, `SELECT`, `UPDATE`, and `DELETE` on an
  existing table; `CREATE TABLE` is refused
  (`permission denied for schema public`); `COPY (SELECT 1) TO PROGRAM
  '...'` is refused (`permission denied to COPY to or from an external
  program ... Only roles with privileges of the "pg_execute_server_program"
  role may`) — the exact mechanism the finding names.
- `sretab_readonly` can `SELECT`; `INSERT` is refused
  (`permission denied for table sources`); `COPY ... TO PROGRAM` is refused
  the same way as above.
- `sretab_migrate` can `CREATE TABLE`, `ALTER TABLE ... ADD COLUMN`, and
  `DROP TABLE`; `COPY ... TO PROGRAM` is refused the same way — DDL rights
  do not imply this one, which is worth stating since it is the one that
  matters.
- A table `sretab_migrate` creates after installation is immediately
  usable by `sretab_app` (`INSERT`/`SELECT` with no manual `GRANT`) and by
  `sretab_readonly` (`SELECT`, including on the new table's sequence) —
  the default-privileges mechanism, exercised rather than only read.
- `pg_dump -U sretab_readonly` against a database with rows already
  inserted and one row deleted (so a table's sequence sits ahead of its
  row count) produces a dump that restores with the sequence intact and
  the next `INSERT` correctly avoiding the deleted id. The negative case
  was checked too, deliberately: a role granted `SELECT` on tables but
  nothing on sequences makes `pg_dump` fail outright —
  `pg_dump: error: failed to get data for sequence "feed_items_id_seq";
  user may lack SELECT privilege on the sequence` — which is a louder
  failure than "restores wrong," but confirms table-`SELECT` alone is not
  enough, which is the point this role's sequence grant exists to cover.
- Rotating `sretab_migrate`'s password via `--rotate` and then connecting
  over a real network path (a second container reaching it by name, the
  way `sre-tab-app` reaches `sre-tab-db` on `sre-tab.network`) confirmed
  the new password works and the superseded one is refused —
  `FATAL: password authentication failed for user "sretab_migrate"`.
  (Note: this only bites over `host` connections. The same check over the
  container's own loopback interface, or via `podman exec`, would have
  passed with *any* password, superseded or not — the image's default
  `pg_hba.conf` trusts `127.0.0.1`/`::1` unconditionally, same as the
  unix socket. Do not use a loopback connection to sanity-check a
  password rotation; it will not catch a wrong one.)
- Re-running `roles.sql` with no role or secret changes produces zero
  `NOTICE` lines from the ownership sweep (nothing left to reassign) and
  succeeds — the idempotency claim above is exercised, not asserted.

Podman was not available on the machine this file was first written on, so
`create-roles.sh`'s shell logic was originally exercised under Docker with
the handful of `podman` subcommands it calls shimmed onto Docker
equivalents — real for everything `psql` executed, a stand-in for
`podman exec`, `podman inspect`, and `podman secret`.

**That caveat is now discharged.** `create-roles.sh` has since been run
unmodified on a real Debian 13 host with podman 5.4.2 — the reference
environment — against a `postgres:18-trixie` container with this
repository's migrations already applied, and the whole decision tree
behaved as documented:

- A first run created the three roles, wrote all three podman secrets, and
  swept every existing table onto `sretab_migrate`. `pg_roles` confirms none
  of the three holds `SUPERUSER`, `CREATEDB`, or `CREATEROLE`.
- A second run with no flags printed "already exists, password left
  unchanged" three times and produced zero `NOTICE` lines from the sweep —
  the idempotency claim above, on the engine that matters.
- Both drift directions refuse rather than guess, and say which way round
  the drift is: deleting `sre-tab-app-database-url` and re-running produced
  "role sretab_app already exists, but sre-tab-app-database-url does not",
  and dropping `sretab_readonly` while its secret remained produced the
  mirror image. `--rotate` resolved both.
- After `--rotate`, connecting as `sretab_migrate` over the container
  network with the password from the rewritten secret succeeds and reports
  `inet_server_addr()` non-null (so it really was a `host` connection, not
  the loopback one that would have accepted anything), while a deliberately
  wrong password is refused with `FATAL: password authentication failed for
  user "sretab_migrate"`.
- `restore.sh` was run end to end on the same host with **no** `PGPASSWORD`
  or `SRE_TAB_RESTORE_URL` in the environment, so both credentials came from
  podman secrets — the branch neither Docker nor CI can reach. It recreated
  the target database as the superuser, re-applied `roles.sql` either side
  of `pg_restore`, restored as `sretab_migrate`, and left all fourteen
  tables owned by `sretab_migrate` with `sretab_app` holding `INSERT` and
  not `TRUNCATE`, and `sretab_readonly` holding `SELECT`.

<a id="the-cutover-itself-was-run"></a>
### The cutover itself was run, on a real Quadlet install

The evidence above is about the roles. This is about the *units*, which is a
different claim and needed its own pass. A Debian 13 host with podman 5.4.2
was taken from nothing — no `/etc/sre-tab`, no secrets, no volumes — through
`install.sh`, `create-secrets.sh`, `create-roles.sh`, and `install.sh --start`
with the cut-over units in place. It is also the first time these Quadlets
have been exercised under live systemd in this workstream, so some of what
follows is worth more than the cutover itself.

- **The stack came up healthy with every unit on its new credential.**
  `install.sh --start` returned after 17.7s on an empty volume;
  `sre-tab-migrate.service` exited zero and stayed `active (exited)`; the
  application and Caddy started; `/api/v1/healthz` returned `"status":"ok"`
  with the database probe green and the scheduler on `postgres-advisory`.
  `systemctl --failed` was empty throughout, including across the stops the
  rollback test performed. (That 17.7s is one host's number on a first start
  with `initdb` in it, recorded because it is what the run produced, not as a
  figure to expect — the deploy-window table in
  [deploy/README.md](README.md#how-long-a-deploy-actually-takes) is the
  measured account of restart timing.)
- **The privilege boundary holds from inside the running application
  container**, which is the finding closed where it actually matters rather
  than in a harness. Connecting through the container's own `DATABASE_URL`,
  as the process that serves requests does: `current_user` is `sretab_app`,
  `is_superuser` is `off`, `CREATE TABLE` is refused (`permission denied for
  schema public`), `COPY (SELECT 1) TO PROGRAM` is refused (`permission
  denied to COPY to or from an external program`), `TRUNCATE sources` is
  refused (`permission denied for table sources`) — and a `DELETE` on
  `sessions`, which the application genuinely needs, succeeds.
- **The migration unit works on `sretab_migrate`, and the default-privilege
  mechanism fires in production shape.** The schema was taken to
  `alembic downgrade base` on that credential and rebuilt by restarting
  `sre-tab-migrate.service`; all fourteen tables came back owned by
  `sretab_migrate`, and `sretab_app` held `SELECT,INSERT,UPDATE,DELETE` and
  `sretab_readonly` `SELECT` on them with **no `GRANT` run in between**.
  `has_sequence_privilege` confirms `sretab_readonly` holds `SELECT` on the
  new sequences, which is the grant `pg_dump` fails without.
- **The application does real work on the DML role**, not only synthetic
  probes: with the scheduler enabled it ingested 257 feed items across all
  seven seeded sources, so the `INSERT`s, the `ON CONFLICT` upserts into
  `source_status`, and `sre-tab seed`'s own writes all run as `sretab_app`.
- **The backup unit produces a restorable dump as `sretab_readonly`,
  sequences included.** `systemctl start sre-tab-backup.service` wrote a
  dump and its `.sha256` sidecar. To make the sequence claim observable, a
  seeded source was deleted first, leaving six rows behind a sequence sitting
  at 7. `restore.sh` brought back six rows with the sequence still at 7, and
  the next `nextval()` returned 8 — so a restored database does not hand out
  an id that a restored row already holds.
- **`sre-tab sessions prune` really deletes on its new credential.** Three
  session rows were planted — one expired, one revoked thirty days ago, one
  live. `systemctl start sre-tab-prune-sessions.service` logged
  `deleted 2 dead session rows` and left the live one. This is the one unit
  whose DML is a `DELETE`, and a read-only misconfiguration there would be
  silent until the table grew.
- **The rollback was executed, not described** — see
  "[Rollback](#rollback)".
- **`deploy/README.md` was then executed rather than proofread**, on a second
  wipe of the same host:
  `python3 .github/scripts/run-doc-examples.py deploy/README.md --root .`
  runs every `docs:run` block in document order, which now includes the
  database-first ordering the roles impose. It completed, and the state it
  left behind had `sretab_app` as the only role connected to the database.
- **The rollout runbook was run against the shape it is written for**, which
  is not a fresh install: the host was rewound to a deployment already up on
  the superuser, with data and with the three roles dropped entirely, and the
  runbook was then followed step by step. The ownership sweep reassigned all
  fourteen existing tables; steps 3 and 4 changed nothing that was running,
  confirmed by asking `pg_stat_activity` between them; step 5 moved the
  application to `sretab_app`; and both timer-driven units were exercised
  immediately afterwards, the backup growing from 128,620 to 135,926 bytes
  across the change rather than collapsing, which is what a missing grant
  would look like if it did not fail outright.
- **The outage that step causes was measured rather than estimated.** Polling
  five times a second across the restart: the API answered `502` for 6.4
  seconds and the SPA document never stopped answering `200`. Caddy is not
  among the units being restarted, so the published port is never withdrawn
  and the netavark hostport tail that dominates a promotion does not occur.
  `systemctl` returned at 16.3 seconds — about ten seconds *after* service
  resumed, which is `Notify=healthy` waiting on the image's healthcheck.

Three things the run contradicted, all now fixed rather than only noted.
`install.sh --start` checked for four secrets that predate the cutover and
none of the three the units now need, so it would have passed and then
watched systemd fail. `smoke.sh`, despite running as the three roles, never
opens a file under `deploy/quadlet`, so a reverted cutover would have sailed
through it. And the runbook's own "which role is connected?" check queried
`pg_stat_activity` without excluding `pg_backend_pid()`, so the superuser
`psql` asking the question counted itself and the answer always contained
`sretab` — a false alarm on the single check the whole procedure turns on,
found by running the document rather than reading it. The first two are gates
now, and both were watched failing on purpose first.

<a id="cutover-procedure"></a>
## Cutover procedure — executed

**The units have been cut over.** No file under `deploy/quadlet/` names the
superuser except `sre-tab-db.container`, which must. The ordered, executable
procedure for doing this to a host that is still running as the superuser is
in [deploy/README.md](README.md#cutting-a-running-deployment-over-to-the-roles);
what follows is the record of what moved and why, which is what makes the
rollback and the next audit cheap.

It landed as its own commit touching nothing but `deploy/quadlet/`, for the
reason the rollback section gives.

### Every consumer of the superuser credential, and where it went

| File | What it does | Role |
| --- | --- | --- |
| `deploy/quadlet/sre-tab-db.container` | `Environment=POSTGRES_USER=sretab` — this line is *why* `sretab` is the superuser; it is the official image's bootstrap-superuser variable, not an ordinary app credential | **unchanged, and must stay so.** The superuser has to keep existing to own the cluster and to be what `create-roles.sh` runs as |
| `deploy/quadlet/sre-tab.container` | `Secret=sre-tab-app-database-url,type=env,target=DATABASE_URL` | **done** — `sretab_app` |
| `deploy/quadlet/sre-tab-migrate.container` | `Secret=sre-tab-migrate-database-url,type=env,target=DATABASE_URL`, runs `alembic upgrade head` | **done** — `sretab_migrate` |
| `deploy/quadlet/sre-tab-prune-sessions.container` | `Secret=sre-tab-app-database-url,type=env,target=DATABASE_URL`, runs `sre-tab sessions prune` (a `DELETE` on `sessions`) | **done** — `sretab_app`, because it is DML; the same role as the application, not the DDL role |
| `deploy/quadlet/sre-tab-backup.container` | `Environment=PGUSER=sretab_readonly` + `Secret=sre-tab-readonly-password,type=env,target=PGPASSWORD`, runs `pg_dump` | **done** — `sretab_readonly` |
| `deploy/quadlet/sre-tab-status.container` | hourly `sre-tab status` | **outstanding** — this unit is not in this branch. It arrives with PR #19 (`deploy/status-alerting`) still naming `sre-tab-database-url`. `sre-tab status` is read-only — `refresh_status` and `nonconforming_slugs` are both `SELECT`s — so it belongs on **`sretab_readonly`**. There is a shape mismatch to solve first: `sre-tab-readonly-password` holds a bare password for `PGPASSWORD`, and the CLI wants a whole `DATABASE_URL`. **Mint a `sre-tab-readonly-database-url` in `create-roles.sh` alongside the bare password** rather than settling for `sretab_app` — giving a read-only job write rights because the secret is the wrong shape is widening a role to suit a format, which is the opposite of what this file is for. Whichever branch merges second owns this row |
| `deploy/install.sh` | **done** — `--start` now refuses until `sre-tab-migrate-database-url`, `sre-tab-app-database-url`, and `sre-tab-readonly-password` exist, and prints the first-install ordering | nothing further |
| `deploy/scripts/create-secrets.sh` | builds `sre-tab-database-url` as `postgresql+psycopg://sretab:...@...`, `--user` defaults to `sretab` | **unchanged, deliberately.** It still writes the superuser's own secrets, `--rotate-db` still needs it, and `sre-tab-database-url` is now read by nothing except a rollback — which is exactly why it must keep being written |
| `deploy/scripts/restore.sh` | **done** — `--user`/`--password-secret` still default to the superuser and cover only `DROP DATABASE`/`CREATE DATABASE`; `pg_restore` runs as `--restore-user`, defaulting to `sretab_migrate` and taking its credential from `sre-tab-migrate-database-url` | nothing further; [see below](#restore-split-credential) for the decision and its reasoning |
| `deploy/scripts/smoke.sh` | **done** — applies `roles.sql` to its throwaway PostgreSQL, runs migrate as `sretab_migrate`, app and session sweep as `sretab_app`, backup as `sretab_readonly`, and now reads the unit files first and refuses to proceed if they disagree | nothing further; [see below](#smoke-tests-the-cutover) |
| `deploy/README.md` | **done** — its "Secrets" table names all seven and marks the two the cutover stopped reading, and it carries the rollout runbook | nothing further |

The `sre-tab-status.container` row is the one open item, and it is open
because the unit does not exist on this branch: a file that is not here
cannot be edited here. It is named rather than left to be discovered, which
is the whole reason this table exists.

<a id="restore-split-credential"></a>
### `restore.sh` takes a split credential — decided, and landed

`ROADMAP.md` called this out by name, and it was genuinely open: restoring a
database is not a DML or even a DDL operation in the schema sense —
`DROP DATABASE` and `CREATE DATABASE ... OWNER ...` are database-level
administrative operations that none of the three roles above is given,
because none of them should be. `sretab_migrate` owns objects *inside* the
`sretab` database; it is not the *owner of* the database, and PostgreSQL does
not conflate the two.

Two ways to close it were on the table, and **option 1 is what landed**: keep
a separate administrative credential — the superuser that already exists —
used only for the `DROP DATABASE`/`CREATE DATABASE` step, and run the actual
`pg_restore` as `sretab_migrate`, which needs exactly the rights
`alembic upgrade` needs and nothing beyond them.

The other option was granting `sretab_migrate` `CREATEDB` so that one
credential could do both, and it is worth saying why it was not taken, since
it is the cheaper change to write and the more expensive one to live with.
`CREATEDB` is cluster-wide and permanent: it would widen the role the
migration unit runs **unattended on every deploy**, and let it create *other*
databases besides this one, in exchange for convenience in a break-glass
procedure that a human runs with host root already in hand. The superuser
credential has to keep existing either way — the rollback below depends on
`sre-tab-database-url` and `sre-tab-postgres-password` staying untouched — so
option 1 costs nothing new and grants nothing new, while option 2 would have
bought a one-flag simplification at the price of a permanent widening.

| Flag | Default | Used for |
| --- | --- | --- |
| `--user` / `--password-secret` | `sretab` / `sre-tab-postgres-password` | `DROP DATABASE`, `CREATE DATABASE`, and re-applying `roles.sql` |
| `--restore-user` / `--restore-url-secret` | `sretab_migrate` / `sre-tab-migrate-database-url` | `pg_restore` itself |

Setting `--restore-user` to the same value as `--user` collapses the two back
into the single credential the script used before, which is the supported
path on a host where the roles were never installed.

Three things about that are not obvious from the flags alone:

- **The restore role's credential arrives as a whole `DATABASE_URL`**, not as
  a bare password, because that is the secret `create-roles.sh` mints for it;
  a second secret holding the same password in another shape would be one
  more thing to keep in step. The password is lifted out of the URL *inside*
  the client container and exported as `PGPASSWORD` — passing the URI to
  `pg_restore --dbname` instead would publish the password in the host's
  process table, which is the thing the podman secret exists to prevent. The
  role named in the URL is checked against `--restore-user` rather than
  trusted: the two arrive from different flags, and disagreeing about them
  otherwise surfaces as `password authentication failed for user ...`, which
  reads as a rotation problem and is not one.
- **`roles.sql` is re-applied to the new database, on both sides of the
  restore.** Grants and default privileges live *inside* a database and the
  restore drops it, so every table-level `GRANT` and every `pg_default_acl`
  row goes with it. The pass *before* `pg_restore` is what makes the restore
  possible at all — it gives `sretab_migrate` `CREATE` on schema `public`,
  without which the first `CREATE TABLE` in the dump is refused, and it
  re-establishes `ALTER DEFAULT PRIVILEGES FOR ROLE sretab_migrate` so that
  every table the restore creates arrives with `sretab_app`'s DML grants and
  `sretab_readonly`'s `SELECT` already attached. The pass *after* it covers
  what the first cannot: a restore run with `--restore-user sretab` leaves
  every table owned by the superuser, which those default privileges do not
  reach, and the ownership sweep is what puts that right. On the default path
  the second pass is a genuine no-op — which is worth having as well, because
  it means every restore exercises the idempotency the install path relies
  on.
- **A missing role is an error before anything is dropped.** The restore role
  is checked on the administrative connection, ahead of the confirmation
  prompt, and the message names both ways out: install the roles, or pass
  `--restore-user sretab` on a host that has not been cut over. Finding out
  after `DROP DATABASE` would mean the failure had already destroyed the
  thing it was about to fail on.

<a id="smoke-tests-the-cutover"></a>
### `smoke.sh` tests the cutover rather than routing around it

It used to connect as nothing but the superuser, so it would have kept
reporting success through a half-done or silently reverted cutover — it was
not capable of catching a regression in this area at all. It now:

0. **Reads the four unit files before it starts a single container**, and
   fails if any of them names a credential other than the one the
   corresponding container below is about to be handed. This step is listed
   zeroth because it is the one that makes the rest of the list mean what it
   says, and because for a while this document claimed the rest of the list
   already did. It did not: the harness has no podman secrets — under
   `CONTAINER_ENGINE=docker` it cannot have any — so it invents its own
   connection strings, and every assertion below would have gone on passing
   with all four units reverted to the superuser, because nothing in the file
   ever opened one. That is precisely the shape of green check this
   repository has shipped six times: a gate reporting success about something
   it does not read. The check also refuses a unit that consumes
   `sre-tab-database-url` again, and insists `sre-tab-db.container` still
   carries `POSTGRES_USER=sretab` — which is not a relaxation but the role
   that owns the cluster. Watched failing on a real host under four separate
   mutations: the whole cutover reverted, the session sweep left behind, the
   backup moved to `PGUSER=sretab_readonly` while still holding the
   superuser's password secret, and the migration unit handed `sretab_app`.
   Each named the file and the line.
1. **Applies `roles.sql` against its own throwaway PostgreSQL**, before the
   migrations rather than after. That makes `sretab_migrate` the role running
   every `CREATE TABLE` from the start, which is both the post-cutover steady
   state and the only arrangement in which `ALTER DEFAULT PRIVILEGES` is load
   bearing. The SQL goes through `psql` directly rather than through
   `create-roles.sh`, which writes podman secrets and would tie the file to
   one engine — `CONTAINER_ENGINE=docker` is a supported configuration and CI
   runs podman. The discipline is kept either way: the three passwords reach
   `psql` over stdin as `\set` variables, never as literals in the SQL and
   never on a command line.
2. **Runs the migration container as `sretab_migrate`, the application and
   `sre-tab sessions prune` as `sretab_app`, and the backup as
   `sretab_readonly`.** Each role gets a different password, deliberately: a
   shared one would let a container handed the wrong `DATABASE_URL` connect
   anyway, and every assertion below would pass while testing the wrong
   thing. Nothing but the superuser `psql` helper and `restore.sh`'s
   `DROP`/`CREATE DATABASE` step connects as `sretab`.
3. **Asserts the refusals by their text**, not merely by a non-zero exit: a
   `psql` that fails from a typo, the wrong database, or a connection it
   never made would otherwise read as a passing negative assertion.
   `sretab_app` cannot `CREATE TABLE` (`permission denied for schema
   public`); none of the three can `COPY ... TO PROGRAM` (the
   `pg_execute_server_program` refusal — the mechanism this whole document is
   about, asserted for the DDL role too); `sretab_readonly` cannot `INSERT`.
4. **Exercises the default-privileges mechanism rather than reading it.** A
   table `sretab_migrate` creates is immediately writable by `sretab_app` and
   readable by `sretab_readonly`, sequence included, with no `GRANT` anywhere
   in the test. This is the piece most likely to be silently misconfigured,
   because naming the wrong role in `FOR ROLE` applies without error and then
   simply never fires — and the assertion was watched failing with exactly
   that mutation before it was believed.
5. **Restores through the split credential**, so the `restore.sh` path above
   is covered on every run, and re-checks after the restore that the tables
   came back owned by `sretab_migrate` and that `sretab_app` can read them
   but still cannot `CREATE TABLE`.

Every table in `public` is also checked to be owned by `sretab_migrate` after
the migration, and the three roles are checked to hold none of `SUPERUSER`,
`CREATEDB`, `CREATEROLE`, `REPLICATION`, or `BYPASSRLS`.

## Rotating a role's password

`--rotate` used to be unconditionally safe, because nothing read these
secrets. It no longer is, and the reason is the one operational fact worth
carrying out of this whole document: **a running container does not pick up a
changed podman secret.** The secret is read once, at container start, and
injected into the environment; rotating it changes what the *next* start
sees and nothing about the process that is running. So a rotation without a
restart leaves a unit holding a password the database no longer accepts, and
the failure arrives at the next restart — which might be a reboot, weeks
later, with nothing connecting the two events.

It is the same shape as rotating the superuser's password
(`create-secrets.sh --rotate-db`, documented in
[deploy/README.md](README.md)). Rotating a role that is in active use means,
in order: run `create-roles.sh --rotate`, then restart every unit that
consumes a secret that changed. `--rotate` changes all three at once, so
that is every unit:

```bash
sudo deploy/scripts/create-roles.sh --rotate
sudo systemctl restart sre-tab-migrate.service sre-tab.service
```

`sre-tab-prune-sessions.service` and `sre-tab-backup.service` need no restart
and cannot usefully take one: they are timer-driven oneshots that are not
running, so each picks up the new secret at its next elapse. Restarting them
does not stage anything — it runs the job.

Note that `sre-tab.service` and `sre-tab-prune-sessions.service` share
`sre-tab-app-database-url`, which is why they are named together anywhere a
narrower rotation is ever added.

<a id="rollback"></a>
## Rollback

If the cutover misbehaves, reverting is deliberately cheap, because
nothing about installing these roles ever touched the superuser's own
credential: `sre-tab-database-url` and `sre-tab-postgres-password` are
untouched by anything in this file and keep working throughout. Do not
delete either as part of the cutover — leaving them in place *is* the
rollback path.

To roll back:

1. Revert the commit that changed the `Secret=`/`Environment=` lines in
   `deploy/quadlet/` (the cutover landed as its own commit for exactly this
   reason — see `deploy/scripts/promote.sh`'s "commit, then `install.sh`"
   pattern for the shape it follows).
2. `sudo deploy/install.sh` to regenerate the systemd units from the
   reverted Quadlet files.
3. `sudo systemctl restart sre-tab.service sre-tab-migrate.service
   sre-tab-prune-sessions.service sre-tab-backup.service` — every unit the
   cutover touched. The last two are oneshots, so restarting them *runs*
   them; that is intended here, because it is what proves they work on the
   reverted credential rather than merely being staged on it.

**This has been executed, on the reference host, against a live install.**
`git revert` of the cutover commit, `install.sh`, that one `systemctl
restart`: the application came back reporting `current_user=sretab` with
`is_superuser=on` from inside the container, `/api/v1/healthz` answered
`"status":"ok"`, the backup wrote a dump, the session sweep deleted two rows,
and `systemctl --failed` stayed empty. Re-applying the cutover afterwards
(revert of the revert, `install.sh`, restart) returned the application to
`sretab_app`. A rollback procedure nobody has run is a paragraph, not a
procedure; this one is a procedure.

One detail the run made concrete: `install.sh --start`'s new preflight still
demands the three role secrets after a rollback, and that is correct rather
than an obstacle — they still exist, because the rollback does not delete
them.

The three non-superuser roles and their secrets are harmless to leave in
place after a rollback; nothing references them once the `Secret=` lines
are reverted, and the next cutover attempt can reuse them as they are.
