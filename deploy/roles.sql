-- Non-superuser roles for the sre-tab database.
--
-- ROADMAP.md's one finding whose severity the operator count does not cap:
-- the application, the migration unit, and the backup all connect as
-- POSTGRES_USER=sretab, which the official postgres image creates as the
-- cluster superuser. An application-level SQL injection therefore does not
-- stop at the tables it can reach through the ORM — `COPY ... PROGRAM` is
-- available to a superuser, and it executes commands. This file creates the
-- three roles that close that: DDL for `alembic upgrade`, DML for the
-- application, and read-only for `pg_dump`. None of them is superuser, and
-- none can create a database or another role.
--
-- Installing this script does NOT change what DATABASE_URL, PGUSER, or any
-- Quadlet unit points at. The three roles it creates are not referenced
-- anywhere in deploy/quadlet until a later, deliberate cutover — see
-- deploy/ROLES.md for that procedure.
--
-- Run through one of the three scripts that know how to feed it, never by
-- hand: create-roles.sh installs the roles and rotates their passwords,
-- restore.sh re-applies the grants and default privileges that a
-- DROP DATABASE takes with it, and smoke.sh installs them against its own
-- throwaway PostgreSQL. Each supplies the `\set` variables this file reads
-- (the three role passwords and the three `set_password_*` flags) over
-- psql's stdin, never as literals here and never on a command line. The two
-- that only re-apply grants pass `set_password_* false`, which is also the
-- path create-roles.sh takes on a re-run against a role that already exists.
--
-- Run as: the existing cluster superuser (sretab, by default) against the
-- sretab database. Idempotent — every statement below is either a genuine
-- no-op on repeat (GRANT, ALTER ROLE without PASSWORD, ALTER DEFAULT
-- PRIVILEGES) or is guarded by an explicit existence check (CREATE ROLE,
-- the ownership sweep). Nothing here revokes the superuser's own access, so
-- the application, migration, and backup units keep working exactly as
-- they do today.
--
-- Roles
-- -----
--   sretab_migrate  DDL.  Runs `alembic upgrade head`. CREATE/USAGE on the
--                   public schema, and owns every table and sequence in it
--                   (see "Ownership" below), which is what lets it ALTER
--                   and DROP them too — PostgreSQL has no separate
--                   GRANT-able "ALTER" or "DROP" privilege on a table; both
--                   require ownership. Not superuser, cannot create
--                   databases or other roles.
--   sretab_app      DML.  The application's day-to-day connection, and
--                   `sre-tab sessions prune`'s. SELECT/INSERT/UPDATE/DELETE
--                   on every table, USAGE and SELECT on every sequence
--                   (USAGE so a SERIAL primary key's implicit nextval()
--                   works on INSERT; SELECT so the current value can be
--                   read back) — and nothing else. No DDL, no TRUNCATE.
--   sretab_readonly Read-only, backs `pg_dump`. SELECT on every table AND
--                   every sequence. Table SELECT alone is not enough: a
--                   full-format dump also emits a `setval()` call per
--                   sequence so a restore's next INSERT does not collide
--                   with rows the dump just loaded, and reading a
--                   sequence's current value for that needs SELECT on the
--                   sequence itself — USAGE (which covers nextval/currval)
--                   does not cover it.
--
-- Ownership
-- ---------
-- Schema `public` is left owned by whatever owns it today (the cluster
-- bootstrap superuser, via the `pg_database_owner` pseudo-role — confirmed
-- against a real postgres:18 container rather than assumed). sretab_migrate
-- is granted CREATE and USAGE on it rather than made its owner: the
-- database is still fully controlled by the superuser at this point in the
-- rollout, so there is no reason to reassign the schema itself and every
-- reason to keep this script's footprint to what the finding actually
-- requires.
--
-- Every table and sequence in `public` — the ones that exist already and
-- every one `alembic upgrade` creates from here on — is owned by
-- sretab_migrate. The ones that exist already were created by the
-- superuser before this script ever ran, so the sweep below reassigns them
-- once (a no-op on repeat: it only touches objects it does not already
-- own). The future ones need no such sweep: once the migration unit
-- connects as sretab_migrate instead of the superuser (the cutover in
-- deploy/ROLES.md), it is the role doing the CREATE TABLE, so it is the
-- owner from the moment the table exists — which is also exactly why the
-- `ALTER DEFAULT PRIVILEGES` below is written `FOR ROLE sretab_migrate`:
-- default privileges attach to the role that creates an object, not to
-- whoever happens to run the ALTER DEFAULT PRIVILEGES statement (that
-- would be the superuser, here, which is not the role that will ever
-- create a table again after cutover).

\set ON_ERROR_STOP on

-- === Roles ================================================================
--
-- Deliberately not DO blocks: psql's `:'var'` substitution is skipped
-- inside dollar-quoted bodies (verified against a real postgres:18 — a DO
-- block referencing `:'migrate_password'` sends the literal text
-- `:'migrate_password'` to the server and fails to parse), so the
-- shell-supplied passwords have to be interpolated into plain top-level
-- statements instead. The role-exists check therefore also has to happen
-- client-side, via `\gset` pulling the server's answer into a psql
-- variable that `\if` can then branch on.

SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sretab_migrate')
    AS migrate_exists \gset
\if :set_password_migrate
    \if :migrate_exists
        ALTER ROLE sretab_migrate PASSWORD :'migrate_password';
        \echo 'sretab_migrate: password rotated'
    \else
        CREATE ROLE sretab_migrate LOGIN PASSWORD :'migrate_password';
        \echo 'sretab_migrate: role created'
    \endif
\else
    \if :migrate_exists
        \echo 'sretab_migrate: already exists, password left unchanged'
    \else
        -- \quit does not take an exit-status argument on every psql this
        -- runs against (verified: it silently ignores one and exits 0,
        -- which create-roles.sh cannot detect) — RAISE EXCEPTION does, and
        -- ON_ERROR_STOP turns it into the non-zero exit the wrapper script
        -- checks for.
        DO $$ BEGIN
            RAISE EXCEPTION 'sretab_migrate does not exist and no password was supplied to create it (this means create-roles.sh believed the role and its podman secret were already in sync -- re-run it with --rotate to resolve the drift)';
        END $$;
    \endif
\endif

SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sretab_app')
    AS app_exists \gset
\if :set_password_app
    \if :app_exists
        ALTER ROLE sretab_app PASSWORD :'app_password';
        \echo 'sretab_app: password rotated'
    \else
        CREATE ROLE sretab_app LOGIN PASSWORD :'app_password';
        \echo 'sretab_app: role created'
    \endif
\else
    \if :app_exists
        \echo 'sretab_app: already exists, password left unchanged'
    \else
        DO $$ BEGIN
            RAISE EXCEPTION 'sretab_app does not exist and no password was supplied to create it (re-run create-roles.sh with --rotate to resolve the drift)';
        END $$;
    \endif
\endif

SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sretab_readonly')
    AS readonly_exists \gset
\if :set_password_readonly
    \if :readonly_exists
        ALTER ROLE sretab_readonly PASSWORD :'readonly_password';
        \echo 'sretab_readonly: password rotated'
    \else
        CREATE ROLE sretab_readonly LOGIN PASSWORD :'readonly_password';
        \echo 'sretab_readonly: role created'
    \endif
\else
    \if :readonly_exists
        \echo 'sretab_readonly: already exists, password left unchanged'
    \else
        DO $$ BEGIN
            RAISE EXCEPTION 'sretab_readonly does not exist and no password was supplied to create it (re-run create-roles.sh with --rotate to resolve the drift)';
        END $$;
    \endif
\endif

-- Non-password attributes, enforced unconditionally every run so that a
-- manual `ALTER ROLE` drifting one of these away from the intended shape
-- (someone granting sretab_app CREATEDB by hand, say) is corrected the next
-- time this script runs rather than persisting silently.
ALTER ROLE sretab_migrate  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS LOGIN;
ALTER ROLE sretab_app      NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS LOGIN;
ALTER ROLE sretab_readonly NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS LOGIN;

-- === Membership assertion ==================================================
--
-- CREATE ROLE above only ever runs for a role that did not already exist;
-- a role that was already there — carrying whatever membership someone
-- granted it by hand, or that a prior, unrelated deployment left behind —
-- is never recreated, so that membership survives everything above
-- untouched. NOSUPERUSER blocks the direct route to `COPY ... TO PROGRAM`,
-- but PostgreSQL also grants it via membership of the predefined role
-- pg_execute_server_program, and pg_read_server_files / pg_write_server_files
-- are the same shape of problem for reading and writing arbitrary files on
-- the server's filesystem. A role that reaches any of these through
-- inherited membership defeats the point of this file exactly as much as
-- one left SUPERUSER would.
--
-- Checked against the reserved `pg_` prefix rather than naming the three
-- predefined roles individually, for two reasons. First, PostgreSQL refuses
-- to let anyone create a role whose name starts with "pg_" (`role name
-- "pg_meddle" is reserved`), so the prefix identifies predefined roles
-- precisely and cannot false-positive on a role an operator or another tool
-- created. Second, it also catches whichever predefined role of this shape
-- a future PostgreSQL version adds — `pg_maintain` in PG17 is the precedent
-- for "a new predefined role turns out to matter here" — without this file
-- needing to know its name in advance. The trade-off is that it also aborts
-- on membership of a predefined role that grants nothing dangerous (say,
-- pg_monitor); that is deliberate, not an oversight — none of the three
-- roles this file creates has any legitimate reason to inherit *any*
-- predefined-role membership, so there is no normal-path case for it to
-- false-positive against, and treating "unexplained" the same as
-- "dangerous" is the cheaper mistake to make.
--
-- Fails closed rather than revoking automatically: an unexpected membership
-- here means someone granted it deliberately, for a reason this script has
-- no way to know, and silently undoing that decision is a worse outcome
-- than stopping and asking a human to look — the same reasoning
-- create-roles.sh uses for a role/secret pair that has drifted apart.
--
-- Idempotent like everything else here: the query only ever finds rows when
-- an unwanted membership actually exists, so a clean run never raises, and
-- re-running after the membership is revoked by hand raises nothing either.
DO $$
DECLARE
    bad RECORD;
BEGIN
    FOR bad IN
        SELECT r.rolname AS member_role, g.rolname AS granted_role
        FROM pg_catalog.pg_auth_members m
        JOIN pg_catalog.pg_roles r ON r.oid = m.member
        JOIN pg_catalog.pg_roles g ON g.oid = m.roleid
        WHERE r.rolname IN ('sretab_migrate', 'sretab_app', 'sretab_readonly')
          AND g.rolname LIKE 'pg\_%'
    LOOP
        RAISE EXCEPTION '% is a member of predefined role % -- this file exists to remove exactly this capability, and inherited membership reaches it as surely as SUPERUSER would; this was not granted by this script, so it will not be silently revoked either. Establish why the membership is there, then drop it by hand (REVOKE % FROM %) and re-run', bad.member_role, bad.granted_role, bad.granted_role, bad.member_role;
    END LOOP;
END
$$;

-- === Schema ================================================================

GRANT CREATE, USAGE ON SCHEMA public TO sretab_migrate;
GRANT USAGE ON SCHEMA public TO sretab_app;
GRANT USAGE ON SCHEMA public TO sretab_readonly;

-- Defence in depth: PostgreSQL 15+ already revokes CREATE on `public` from
-- PUBLIC by default, but this does not assume the cluster's default —
-- POSTGRES_INITDB_ARGS could in principle change it — it says so outright.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- === Existing-object ownership =============================================
--
-- One-off sweep: reassign every table already in `public` to sretab_migrate,
-- so that after cutover it can ALTER and DROP them, not only the tables a
-- future `alembic upgrade` creates. Idempotent by construction — it only
-- touches objects it does not already own, so re-running finds nothing to
-- do.
--
-- Sequences are deliberately NOT swept here. Every sequence this schema has
-- backs a SERIAL primary key, and PostgreSQL links such a sequence to its
-- column with an internal dependency that makes it follow the table's
-- owner automatically — confirmed against a real postgres:18, where
-- `ALTER SEQUENCE sources_id_seq OWNER TO ...` is flatly refused with
-- "Sequence is linked to table", and reassigning `sources` itself changed
-- `sources_id_seq`'s owner too, with no separate statement. Attempting it
-- explicitly for a linked sequence is not redundant, it errors. A future
-- migration that creates a genuinely standalone sequence (one that is not
-- SERIAL/IDENTITY on a column) would need its own ALTER SEQUENCE OWNER TO —
-- there is no such sequence in this schema today.
DO $$
DECLARE
    tbl RECORD;
BEGIN
    FOR tbl IN
        SELECT c.relname
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind = 'r'  -- ordinary tables only; see note above on sequences
          AND pg_catalog.pg_get_userbyid(c.relowner) <> 'sretab_migrate'
    LOOP
        EXECUTE format('ALTER TABLE public.%I OWNER TO sretab_migrate', tbl.relname);
        RAISE NOTICE 'reassigned public.% to sretab_migrate', tbl.relname;
    END LOOP;
END
$$;

-- === Privileges on existing objects ========================================
--
-- ALTER DEFAULT PRIVILEGES (below) only ever applies to objects created
-- after it runs. These cover the ones that exist right now; they are
-- ordinary GRANTs, harmless and idempotent to repeat.

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sretab_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sretab_app;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO sretab_readonly;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO sretab_readonly;

-- === Default privileges for future objects =================================
--
-- `FOR ROLE sretab_migrate` is the load-bearing part: default privileges
-- attach to the role that CREATEs the object, not to whoever runs this
-- ALTER DEFAULT PRIVILEGES statement (the superuser, here). Get this wrong
-- — omit it, or name the wrong role — and it silently does nothing, because
-- the superuser is not the role that will ever create a table again once
-- the migration unit is cut over to sretab_migrate.

ALTER DEFAULT PRIVILEGES FOR ROLE sretab_migrate IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sretab_app;
ALTER DEFAULT PRIVILEGES FOR ROLE sretab_migrate IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO sretab_app;

ALTER DEFAULT PRIVILEGES FOR ROLE sretab_migrate IN SCHEMA public
    GRANT SELECT ON TABLES TO sretab_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE sretab_migrate IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO sretab_readonly;
