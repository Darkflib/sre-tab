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
separated decisions — the cutover that switches the deployment over to them.
That cutover has not happened: every unit under `deploy/quadlet/` still
connects as the superuser, and nothing described under "Installing" below
alters what any running container connects as. What has landed ahead of it is
the operator-facing half — `restore.sh` restores with a split credential, and
`smoke.sh` runs its whole throwaway stack as the three roles and asserts what
they may not do — so that the commit which finally changes the `Secret=`
lines is proved by CI rather than merely believed.

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
| `sre-tab-migrate-database-url` | a `DATABASE_URL` for `sretab_migrate` | `restore.sh`, today; the migration unit, post-cutover |
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

## Cutover procedure (a later, deliberate iteration)

**The units have not been cut over.** `DATABASE_URL`, `PGUSER`, and every
file under `deploy/quadlet/` still name the superuser, and switching them is
its own commit for the reason the rollback section gives.

What *has* landed is the preparation the two subsections below describe:
`restore.sh` now takes a split credential, and `smoke.sh` runs the whole
stack as the three roles and asserts what they may not do. That ordering is
deliberate — it means CI can prove the cutover rather than route around it,
and the commit that changes the `Secret=` lines is then the smallest thing it
can be.

This section exists so that whoever does the cutover has the complete list of
what currently uses the superuser credential, rather than finding the last
one in production — which is exactly the failure mode it is written to
prevent.

### Every current consumer of the superuser credential

| File | What it does today | Role it should move to |
| --- | --- | --- |
| `deploy/quadlet/sre-tab-db.container` | `Environment=POSTGRES_USER=sretab` — this line is *why* `sretab` is the superuser; it is the official image's bootstrap-superuser variable, not an ordinary app credential | stays as-is; the superuser has to keep existing to own the cluster and to run `create-roles.sh` against it |
| `deploy/quadlet/sre-tab.container` | `Secret=sre-tab-database-url,type=env,target=DATABASE_URL` | `sre-tab-app-database-url` → `sretab_app` |
| `deploy/quadlet/sre-tab-migrate.container` | `Secret=sre-tab-database-url,type=env,target=DATABASE_URL`, runs `alembic upgrade head` | `sre-tab-migrate-database-url` → `sretab_migrate` |
| `deploy/quadlet/sre-tab-prune-sessions.container` | `Secret=sre-tab-database-url,type=env,target=DATABASE_URL`, runs `sre-tab sessions prune` (a `DELETE` on `sessions`) | `sre-tab-app-database-url` → `sretab_app` — it is a DML operation, the same role as the application |
| `deploy/quadlet/sre-tab-backup.container` | `Environment=PGUSER=sretab` + `Secret=sre-tab-postgres-password,type=env,target=PGPASSWORD`, runs `pg_dump` | `Environment=PGUSER=sretab_readonly` + `Secret=sre-tab-readonly-password,type=env,target=PGPASSWORD` |
| `deploy/scripts/create-secrets.sh` | builds `sre-tab-database-url` as `postgresql+psycopg://sretab:...@...`, `--user` defaults to `sretab` | not itself part of the cutover — it still needs to exist for the superuser's own secrets and for `--rotate-db` — but its defaults document the pre-cutover assumption and are worth re-reading when this file's own defaults change |
| `deploy/scripts/restore.sh` | **done** — `--user`/`--password-secret` still default to the superuser and now cover only `DROP DATABASE`/`CREATE DATABASE`; `pg_restore` runs as `--restore-user`, defaulting to `sretab_migrate` and taking its credential from `sre-tab-migrate-database-url` | nothing further; [see below](#restore-split-credential) for the decision and its reasoning |
| `deploy/scripts/smoke.sh` | **done** — applies `roles.sql` to its throwaway PostgreSQL and runs migrate as `sretab_migrate`, app and session sweep as `sretab_app`, backup as `sretab_readonly`, with the negative assertions [below](#smoke-tests-the-cutover) | nothing further; a cutover that is half-done or silently reverted now fails CI |
| `deploy/README.md` | its "Secrets" table lists the four `create-secrets.sh` writes, plus `sre-tab-migrate-database-url` as the one role secret something reads today (`restore.sh`) | add the other two to that table once they are live; note the two that stop being read by anything once the corresponding unit's `Secret=` line changes |

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

Before cutover, `deploy/scripts/create-roles.sh --rotate` is unconditionally
safe. No running unit reads `sre-tab-migrate-database-url`,
`sre-tab-app-database-url`, or `sre-tab-readonly-password`, so there is
nothing to break by changing what they contain. `restore.sh` reads the first
of the three, but it reads it at the moment it runs and holds nothing across
a rotation.

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
   sre-tab-prune-sessions.service sre-tab-backup.service` — every unit the
   cutover touched.

The three non-superuser roles and their secrets are harmless to leave in
place after a rollback; nothing references them once the `Secret=` lines
are reverted, and the next cutover attempt can reuse them as they are.
