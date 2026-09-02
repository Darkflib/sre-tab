# Database roles

`deploy/roles.sql` and `deploy/scripts/create-roles.sh` close the one
finding in `ROADMAP.md` whose severity the operator count does not cap: the
application, the migration unit, and the backup all connect to PostgreSQL
as `POSTGRES_USER=sretab`, which the official postgres image creates as the
cluster superuser. An application-level SQL injection therefore does not
stop at the tables the ORM reaches — `COPY ... PROGRAM` is available to a
superuser, and it executes commands.

This document covers what the three roles are, how to install them, and —
because installing them and using them are two different, deliberately
separated decisions — the cutover that actually switches something over to
them, which is later work and not part of this change. Nothing described
under "Installing" below alters what any running container connects as.

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
`alembic upgrade` creates, automatically, because `sretab_migrate` will be
the role running `CREATE TABLE` once the migration unit is cut over.

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
never fire, because the superuser is not the role that will ever create a
table again once the migration unit is cut over to `sretab_migrate`.

## Installing

```bash
sudo deploy/scripts/create-roles.sh
```

This creates the three roles (if they do not already exist), applies every
grant in `roles.sql`, and writes three podman secrets that a later cutover
will consume:

| Podman secret | Holds | For |
| --- | --- | --- |
| `sre-tab-migrate-database-url` | a `DATABASE_URL` for `sretab_migrate` | the migration unit, post-cutover |
| `sre-tab-app-database-url` | a `DATABASE_URL` for `sretab_app` | the application and prune-sessions units, post-cutover |
| `sre-tab-readonly-password` | just the password, for `PGPASSWORD` | the backup unit, post-cutover, alongside `PGUSER=sretab_readonly` |

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

**Nothing currently running changes.** `DATABASE_URL`, `PGUSER`, and every
file under `deploy/quadlet/` are untouched by this script. It is not called
from `install.sh`, deliberately: creating the roles is a different decision
from switching anything over to them, and the second one is the cutover
below.

Re-running it is safe. With no flags, a role or secret that already exists
is left exactly as it is — no password is rotated, nothing is overwritten
— while every grant is re-applied regardless, which is harmless (`GRANT`
and `ALTER DEFAULT PRIVILEGES` are no-ops when already in place) and is
what makes it safe to run again after a schema change, or after a change
to this tooling itself. `--rotate` regenerates all three passwords and
secrets together; see "Rotating a role's password" below for what that
means once one of them is actually in use.

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
  surfacing only later when the cutover starts a unit whose `Secret=`
  reference does not resolve.

## Verification

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

Podman itself was not available on the machine this was written on
(Docker was; `create-roles.sh`'s shell logic — the exists/drift/rotate
decision tree and the secret writes — was exercised against the same
container by shimming the handful of `podman` subcommands it calls onto
Docker equivalents). The SQL in `roles.sql` runs unmodified either way,
since `create-roles.sh` only ever feeds it to `psql` over stdin; the shim
stood in for `podman exec`, `podman inspect`, and `podman secret`, not for
anything `psql` executed. Re-running `create-roles.sh` itself against a
real podman host once one is available is worth doing before the cutover
below, even though the mechanism it drives has already been checked
directly.

## Cutover procedure (a later, deliberate iteration)

Not part of this change. `DATABASE_URL`, `PGUSER`, and every Quadlet unit
still name the superuser. This section exists so that whoever does the
cutover has the complete list of what currently uses the superuser
credential, rather than finding the last one in production — which is
exactly the failure mode this section is written to prevent.

### Every current consumer of the superuser credential

| File | What it does today | Role it should move to |
| --- | --- | --- |
| `deploy/quadlet/sre-tab-db.container` | `Environment=POSTGRES_USER=sretab` — this line is *why* `sretab` is the superuser; it is the official image's bootstrap-superuser variable, not an ordinary app credential | stays as-is; the superuser has to keep existing to own the cluster and to run `create-roles.sh` against it |
| `deploy/quadlet/sre-tab.container` | `Secret=sre-tab-database-url,type=env,target=DATABASE_URL` | `sre-tab-app-database-url` → `sretab_app` |
| `deploy/quadlet/sre-tab-migrate.container` | `Secret=sre-tab-database-url,type=env,target=DATABASE_URL`, runs `alembic upgrade head` | `sre-tab-migrate-database-url` → `sretab_migrate` |
| `deploy/quadlet/sre-tab-prune-sessions.container` | `Secret=sre-tab-database-url,type=env,target=DATABASE_URL`, runs `sre-tab sessions prune` (a `DELETE` on `sessions`) | `sre-tab-app-database-url` → `sretab_app` — it is a DML operation, the same role as the application |
| `deploy/quadlet/sre-tab-status.container` | `Secret=sre-tab-database-url,type=env,target=DATABASE_URL`, runs `sre-tab status --failures-over 3` hourly | **`sretab_readonly`** — not `sretab_app`; see below. Needs a `DATABASE_URL` secret that does not exist yet |
| `deploy/quadlet/sre-tab-backup.container` | `Environment=PGUSER=sretab` + `Secret=sre-tab-postgres-password,type=env,target=PGPASSWORD`, runs `pg_dump` | `Environment=PGUSER=sretab_readonly` + `Secret=sre-tab-readonly-password,type=env,target=PGPASSWORD` |
| `deploy/scripts/create-secrets.sh` | builds `sre-tab-database-url` as `postgresql+psycopg://sretab:...@...`, `--user` defaults to `sretab` | not itself part of the cutover — it still needs to exist for the superuser's own secrets and for `--rotate-db` — but its defaults document the pre-cutover assumption and are worth re-reading when this file's own defaults change |
| `deploy/scripts/restore.sh` | `--user` defaults to `sretab`; `PGUSER=$db_user` on its throwaway TCP client; `CREATE DATABASE "$database" OWNER "$db_user"`; `DROP DATABASE ... WITH (FORCE)` | **needs a decision, not a mechanical swap — see below** |
| `deploy/scripts/smoke.sh` | starts its own throwaway `postgres:18` with only `POSTGRES_USER=sretab`; every container it launches (app, migrate, the `psql_db` helper) connects as `sretab` | **needs to actually exercise the new roles to remain meaningful — see below** |
| `deploy/README.md` | its "Secrets" table lists `sre-tab-postgres-password` and `sre-tab-database-url` only | add the three new secrets to that table once they are live; note the two that stop being read by anything once the corresponding unit's `Secret=` line changes |

### `sre-tab-status.service` is the one application unit that goes read-only

Every other unit running the application image needs DML or DDL, so the
cutover for them is a straight swap onto `sretab_app` or `sretab_migrate`.
The hourly source health check is not: `sre-tab status` calls exactly two
functions in `app/cli/operations.py`, `refresh_status` and
`nonconforming_slugs`, and both are a single `select()` — the first an
outer join of `sources` against `source_status`, the second a scan of
`sources.slug` and `topics.slug`. `_cmd_status` in `app/cli/__init__.py`
never calls `commit()`, and the session it is handed is closed by the
context manager, which rolls back. There is nothing to widen the role for.

Two consequences worth having on purpose. It is the least-privilege answer
— a monitoring job that runs unattended every hour is the last thing that
should hold write access — and it differs from `sre-tab-prune-sessions`,
which runs from the same image and genuinely needs `DELETE`. The two are
not interchangeable and the table above says so.

**The secret it needs does not exist yet.** `create-roles.sh` writes
`sre-tab-readonly-password` — just a password, for `PGPASSWORD`, because
the only consumer planned for `sretab_readonly` was `pg_dump`, which takes
its credential that way. `sre-tab status` takes a `DATABASE_URL`, like every
other application unit, so cutting it over needs a fourth secret of the
shape `sre-tab-app-database-url` already has:

| Podman secret | Holds | For |
| --- | --- | --- |
| `sre-tab-readonly-database-url` | a `DATABASE_URL` for `sretab_readonly` | `sre-tab-status`, post-cutover |

Adding it is a few lines in `create-roles.sh` beside the three it already
writes — the same `\set` over stdin, the same exists/drift/rotate decision —
and it is named here rather than done here for the reason this whole section
exists: installing a credential and switching something onto it are two
decisions, and the second one is the cutover.

### `restore.sh` needs a decision, not a mechanical swap

`ROADMAP.md` calls this out by name, and it is genuinely unresolved here on
purpose: restoring a database is not a DML or even a DDL operation in the
schema sense — `DROP DATABASE` and `CREATE DATABASE ... OWNER ...` are
database-level administrative operations that none of the three roles in
this file are given, because none of them should be. `sretab_migrate` owns
objects *inside* the `sretab` database; it is not the *owner of* the
database, and PostgreSQL does not conflate the two.

Two ways to close this, neither implemented here:

1. **Keep a superuser (or `CREATEDB`-and-cluster-admin) credential for
   restore specifically**, separate from the three roles above, used only
   by `restore.sh` for the `DROP DATABASE`/`CREATE DATABASE` step. The
   actual `pg_restore` step immediately after could still run as
   `sretab_migrate` (it needs to create every table, after all — the same
   right `alembic upgrade` needs), but the recreate step cannot.
2. **Grant `sretab_migrate` `CREATEDB`** so it can own the database it
   restores into. This is a real widening of what "the DDL role" means —
   worth naming as such rather than doing quietly — since `CREATEDB` is
   cluster-wide, not schema-scoped, and lets the role create *other*
   databases too, not just recreate this one.

Either way, `restore.sh`'s `--user`/`--password-secret` flags already
parameterise the credential it connects as, so the mechanical part (making
it take a different default) is small. The part that needs a human
decision is which credential that should be, and that decision belongs to
whoever does the cutover, with current production traffic and the
operator count in front of them — not to this file.

### `smoke.sh` needs to test the cutover, not route around it

Today `smoke.sh` never uses anything but the superuser, so it would keep
reporting success even if the cutover were half-done or silently reverted
— it is not currently capable of catching a regression in this area at
all. At cutover, it needs to:

1. Run `deploy/scripts/create-roles.sh` (or apply `roles.sql` directly)
   against its own throwaway PostgreSQL, the same way it already runs
   migrations against it.
2. Start the migrate, app, and prune-sessions containers it launches with
   the new roles' `DATABASE_URL`s instead of the superuser's.
3. Run its backup step as `sretab_readonly` rather than `sretab`.
4. Add the negative assertions this file's "Verification" section did by
   hand — `sretab_app` cannot `CREATE TABLE`, cannot `COPY ... PROGRAM` —
   so a future change that widens one of these roles by accident fails CI
   instead of only this document going stale.

## Rotating a role's password

Before cutover, `deploy/scripts/create-roles.sh --rotate` is unconditionally
safe — nothing reads `sre-tab-migrate-database-url`,
`sre-tab-app-database-url`, or `sre-tab-readonly-password` yet, so there is
nothing to break by changing what they contain.

After cutover, it is the same shape as rotating the superuser's password
today (`create-secrets.sh --rotate-db`, documented in `deploy/README.md`):
the secret changes, but the running container does not pick up a changed
podman secret on its own. Rotating a role that is in active use means, in
order: run `create-roles.sh --rotate`, then restart every unit that
consumes the secret that changed (per the table above — e.g. rotating
`sretab_app`'s password means restarting `sre-tab.service` and
`sre-tab-prune-sessions.service`, since both read
`sre-tab-app-database-url`).

## Rollback

If the cutover misbehaves, reverting is deliberately cheap, because
nothing about installing these roles ever touched the superuser's own
credential: `sre-tab-database-url` and `sre-tab-postgres-password` are
untouched by anything in this file and keep working throughout. Do not
delete either as part of the cutover — leaving them in place *is* the
rollback path.

To roll back:

1. Revert the commit that changed the `Secret=`/`Environment=` lines in
   `deploy/quadlet/` (the cutover should land as its own commit for
   exactly this reason — see `deploy/scripts/promote.sh`'s "commit, then
   `install.sh`" pattern for the shape to follow).
2. `sudo deploy/install.sh` to regenerate the systemd units from the
   reverted Quadlet files.
3. `sudo systemctl restart sre-tab.service sre-tab-migrate.service
   sre-tab-prune-sessions.service sre-tab-status.service
   sre-tab-backup.service` — every unit the cutover touched. Note what that
   does to the three timer-driven ones: they are not running, so `restart`
   *starts* them, taking a sweep, a status check, and a backup there and
   then. All three are safe to run at any time, and running them is the
   cheapest way to confirm the reverted credential works — but it is a run,
   not a no-op, and on a large database the backup is the slow one.

The three non-superuser roles and their secrets are harmless to leave in
place after a rollback; nothing references them once the `Secret=` lines
are reverted, and the next cutover attempt can reuse them as they are.
