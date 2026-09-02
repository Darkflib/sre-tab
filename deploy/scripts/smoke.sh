#!/bin/sh
#
# Deployment smoke test: fresh PostgreSQL, migrations, health checks, then a
# backup and a restore verified end to end.
#
# This is acceptance criterion 6 ("the app starts with documented environment
# variables, runs migrations, and passes health checks on a fresh PostgreSQL
# deployment") executed rather than asserted, plus the PRD's "test restore
# before release" gate.
#
# It runs the containers with the same flags the quadlets set — read-only
# root filesystems, dropped capabilities, the same non-root UIDs, the same
# tmpfs mounts — so those choices are exercised even though systemd is not
# involved. What it does NOT cover is quadlet generation, unit ordering, and
# podman secrets; CI validates unit generation separately with
# podman-system-generator --dryrun, and the secret plumbing only exists under
# podman on the deployment host.
#
# It also installs the three least-privilege roles from deploy/roles.sql and
# runs everything below as one of them: the migration container as
# sretab_migrate, the application and the session sweep as sretab_app, the
# backup and the hourly source health check as sretab_readonly. Nothing but
# the throwaway psql helper and restore.sh's DROP/CREATE DATABASE step
# connects as the superuser. That is what makes the negative assertions
# further down meaningful.
#
# It is not, on its own, what stops a reverted cutover from passing CI — and
# that claim was made before it was true. The credentials below are this
# file's own; it has no podman secrets, and under CONTAINER_ENGINE=docker it
# cannot have any. So every assertion here would go on passing with every
# unit pointed back at the superuser, because nothing in this script opened
# them. The first step of the run now does exactly that: it reads
# deploy/quadlet and asserts each unit names the credential the corresponding
# container below is about to be handed. Without it, "smoke.sh proves the
# cutover" is a claim about files this script never looks at.
#
# roles.sql is applied through psql directly rather than by calling
# create-roles.sh, which writes podman secrets and would tie this file to one
# engine. ROLES.md permits either; the discipline that matters is the one this
# file keeps — the role passwords reach psql over stdin as `\set` variables,
# never as literals in the SQL and never on a command line.
#
# Engine-agnostic on purpose: CONTAINER_ENGINE=docker runs it on a developer
# machine, and it defaults to podman for CI and the deployment host.
#
#   CONTAINER_ENGINE=docker SRE_TAB_IMAGE=sre-tab:dev deploy/scripts/smoke.sh
#
# Run it on a host that is not already running the stack: it uses the real
# container names so that the Caddyfile's upstream is tested verbatim.

set -eu

ENGINE=${CONTAINER_ENGINE:-podman}
IMAGE=${SRE_TAB_IMAGE:-sre-tab:smoke}
PORT=${SMOKE_PORT:-18080}

PG_IMAGE=docker.io/library/postgres:18-trixie@sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280
CADDY_IMAGE=docker.io/library/caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648

NET=sre-tab-smoke
ASSETS_VOL=sre-tab-smoke-assets
DB_VOL=sre-tab-smoke-db
DB_PASSWORD=smoke-only-not-a-secret

# One password per role, deliberately different from each other and from the
# superuser's. Sharing one would make a mix-up invisible: a container handed
# the wrong role's URL would still connect, and every assertion below would
# pass while testing the wrong thing.
MIGRATE_PASSWORD=smoke-only-not-a-secret-migrate
APP_PASSWORD=smoke-only-not-a-secret-app
READONLY_PASSWORD=smoke-only-not-a-secret-readonly

# Post-cutover connection strings: the migration unit is the only thing that
# gets DDL rights, the application and the session sweep share the DML role,
# and the backup and the source health check run read-only. The superuser has
# no DATABASE_URL here at all, which is the point — deploy/ROLES.md's cutover
# section.
#
# The read-only role appears twice below, once as a URL and once as a bare
# password, which is not two credentials: pg_dump takes PGPASSWORD and
# `sre-tab status` takes a DATABASE_URL. On a deployed host those are the two
# podman secrets create-roles.sh writes from one generated password, and the
# single $READONLY_PASSWORD here is what stands in for that.
MIGRATE_DATABASE_URL="postgresql+psycopg://sretab_migrate:$MIGRATE_PASSWORD@sre-tab-db:5432/sretab"
APP_DATABASE_URL="postgresql+psycopg://sretab_app:$APP_PASSWORD@sre-tab-db:5432/sretab"
READONLY_DATABASE_URL="postgresql+psycopg://sretab_readonly:$READONLY_PASSWORD@sre-tab-db:5432/sretab"

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
workdir=$(mktemp -d)
backups="$workdir/backups"
mkdir -p "$backups"
# The backup container runs as uid 999 and writes here.
chmod 0777 "$workdir" "$backups"

step() { printf '\n=== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        printf '\n--- container logs (failure) ---\n' >&2
        for c in sre-tab-db sre-tab-app sre-tab-web; do
            printf '\n[%s]\n' "$c" >&2
            "$ENGINE" logs "$c" 2>&1 | tail -40 >&2 || true
        done
    fi
    "$ENGINE" rm --force sre-tab-web sre-tab-app sre-tab-db >/dev/null 2>&1 || true
    "$ENGINE" volume rm --force "$ASSETS_VOL" "$DB_VOL" >/dev/null 2>&1 || true
    "$ENGINE" network rm --force "$NET" >/dev/null 2>&1 || true
    rm -rf "$workdir"
    exit "$status"
}
trap cleanup EXIT INT TERM

psql_db() {
    "$ENGINE" exec --env "PGPASSWORD=$DB_PASSWORD" sre-tab-db \
        psql --quiet --no-psqlrc --tuples-only --no-align \
        --username sretab --dbname sretab "$@"
}

# The same query, as one of the three roles, over the container network —
# deliberately not `$ENGINE exec` into the database container like psql_db
# above. The official image's pg_hba.conf trusts the unix socket and the
# loopback interface unconditionally, so a socket connection proves the
# privilege but never checks the password; this takes the `host` path the
# application takes, so a role whose credential is wrong fails here rather
# than in production.
psql_as() {
    as_role=$1
    as_password=$2
    shift 2
    "$ENGINE" run --rm --network "$NET" \
        --env PGHOST=sre-tab-db --env PGDATABASE=sretab \
        --env "PGUSER=$as_role" --env "PGPASSWORD=$as_password" \
        --read-only --security-opt=no-new-privileges --cap-drop all \
        --user 999:999 --pids-limit 64 \
        "$PG_IMAGE" \
        psql --quiet --no-psqlrc --tuples-only --no-align "$@"
}

# A refusal, not merely a failure. A psql that exits non-zero because of a
# typo, the wrong database, or a connection it never made would otherwise read
# as a passing negative assertion — which is the exact shape of green check
# this repository keeps finding, and the reason every one of these names the
# error text PostgreSQL must produce.
assert_refused() {
    what=$1
    want=$2
    shift 2
    if out=$("$@" 2>&1); then
        fail "$what was permitted, not refused: ${out:-(no output)}"
    fi
    printf '%s' "$out" | grep -q "$want" \
        || fail "$what failed, but not with '$want': $out"
    echo "  refused: $what"
}

# The three role passwords travel over psql's stdin as `\set` variables ahead
# of roles.sql's own text — never as literals in the SQL, never on a command
# line — which is the discipline create-roles.sh keeps and the reason roles.sql
# reads them as variables at all.
apply_roles() {
    {
        printf '\\set set_password_migrate true\n'
        printf '\\set migrate_password %s\n' "$MIGRATE_PASSWORD"
        printf '\\set set_password_app true\n'
        printf '\\set app_password %s\n' "$APP_PASSWORD"
        printf '\\set set_password_readonly true\n'
        printf '\\set readonly_password %s\n' "$READONLY_PASSWORD"
        cat "$repo_root/deploy/roles.sql"
    } | "$ENGINE" exec --interactive --env "PGPASSWORD=$DB_PASSWORD" sre-tab-db \
        psql --quiet --no-psqlrc --set=ON_ERROR_STOP=1 \
        --username sretab --dbname sretab
}

step "The Quadlet units name the credentials this run is about to use"
# First, before a single container starts: a disagreement here is a
# repository bug rather than a runtime failure, and it costs three minutes to
# reach if it is discovered at the backup step instead.
#
# This is the join between two halves that would otherwise never meet. The
# units name podman secrets; this script hands its containers URLs it made up
# itself. Both describe a deployment, and nothing made them describe the same
# one — revert the cutover commit and every assertion below still passes,
# because none of them opens a unit file.
#
# What it deliberately does not check is the other link in the chain: that
# `sre-tab-app-database-url` actually contains a URL for sretab_app. That is
# create-roles.sh's to keep, it only exists on a host with podman secrets, and
# asserting it here would mean grepping a printf format string for a role
# name — a check that breaks on reformatting and holds nothing.
unit_names_credential() {
    unit_file="$repo_root/deploy/quadlet/$1"
    grep -qxF "$2" "$unit_file" || fail \
        "deploy/quadlet/$1 does not contain '$2' — the units and this harness disagree about which role the deployment connects as, so everything below would test a deployment that is not the one shipping"
    echo "  $1: $2"
}
unit_names_credential sre-tab.container \
    'Secret=sre-tab-app-database-url,type=env,target=DATABASE_URL'
unit_names_credential sre-tab-migrate.container \
    'Secret=sre-tab-migrate-database-url,type=env,target=DATABASE_URL'
unit_names_credential sre-tab-prune-sessions.container \
    'Secret=sre-tab-app-database-url,type=env,target=DATABASE_URL'
unit_names_credential sre-tab-backup.container \
    'Environment=PGUSER=sretab_readonly'
unit_names_credential sre-tab-backup.container \
    'Secret=sre-tab-readonly-password,type=env,target=PGPASSWORD'
# The hourly health check: the read-only role in the other shape, and the
# reason there are two secrets for one role. It runs the same application
# image as the session sweep two lines up and must not share its credential —
# `sre-tab status` is two SELECTs, the sweep is a DELETE — so this line is
# what makes swapping them fail here rather than in production.
unit_names_credential sre-tab-status.container \
    'Secret=sre-tab-readonly-database-url,type=env,target=DATABASE_URL'

# The superuser's DATABASE_URL secret still exists on a deployed host, and
# deploy/ROLES.md says to leave it there — it is the rollback. What must not
# happen is a unit consuming it again without the rollback being a deliberate,
# reviewed commit, which is what this catches.
if grep -rl '^Secret=sre-tab-database-url' "$repo_root/deploy/quadlet/" 2>/dev/null | grep .; then
    fail "the unit(s) above still consume the superuser's DATABASE_URL"
fi

# The one place the superuser is still correct. It bootstraps the cluster and
# it is the credential create-roles.sh installs the other three with, so
# losing this line breaks the roles rather than tightening anything.
grep -qxF 'Environment=POSTGRES_USER=sretab' \
    "$repo_root/deploy/quadlet/sre-tab-db.container" \
    || fail "sre-tab-db.container no longer bootstraps the sretab superuser"
echo "  sre-tab-db.container still bootstraps the superuser, which owns the cluster"

step "Preparing $NET"
"$ENGINE" rm --force sre-tab-web sre-tab-app sre-tab-db >/dev/null 2>&1 || true
"$ENGINE" network rm --force "$NET" >/dev/null 2>&1 || true
"$ENGINE" volume rm --force "$ASSETS_VOL" "$DB_VOL" >/dev/null 2>&1 || true
"$ENGINE" network create "$NET" >/dev/null
"$ENGINE" volume create "$ASSETS_VOL" >/dev/null
"$ENGINE" volume create "$DB_VOL" >/dev/null

step "Starting PostgreSQL on a fresh volume"
# Flags mirror deploy/quadlet/sre-tab-db.container — including the absence of
# --security-opt=no-new-privileges, which is not an oversight in either place.
# See the note in that unit: under no_new_privs this image never finishes
# starting on Debian 13 / podman 5.4.2. It happens to survive on the CI
# runner's older kernel, which is exactly why the flag has to be off here too
# — otherwise the smoke harness would keep asserting a configuration the
# deployment cannot use.
"$ENGINE" run --detach --name sre-tab-db --network "$NET" \
    --volume "$DB_VOL:/var/lib/postgresql:rw" \
    --env POSTGRES_USER=sretab \
    --env POSTGRES_DB=sretab \
    --env POSTGRES_INITDB_ARGS=--data-checksums \
    --env "POSTGRES_PASSWORD=$DB_PASSWORD" \
    --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
    --tmpfs /run:rw,nosuid,nodev \
    --tmpfs /var/run/postgresql:rw,nosuid,nodev,mode=0755 \
    --cap-drop all \
    --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
    --cap-add SETGID --cap-add SETUID \
    --pids-limit 256 \
    "$PG_IMAGE" >/dev/null

# --host=127.0.0.1 is load-bearing, and its absence was a real race rather
# than a tidiness point. The official image's entrypoint bootstraps a cluster
# by starting a *temporary* server for initdb and the init scripts, and it
# starts that one with `listen_addresses=''` — reachable on the unix socket
# and on nothing else. A pg_isready with no host talks to that socket, so it
# answers "ready" during the bootstrap, and the entrypoint then shuts the
# temporary server down to start the real one. Whatever connected next got
# `FATAL: the database system is shutting down`.
#
# The race has been here all along and only started biting when a step was
# added that connects immediately: applying roles.sql. Migrations, which used
# to be next, run in a container that takes long enough to start that the
# restart had always finished first. Asking over TCP is what distinguishes
# the two servers, because only the real one is listening there.
attempt=0
until "$ENGINE" exec sre-tab-db \
    pg_isready --quiet --host=127.0.0.1 --username=sretab --dbname=sretab; do
    attempt=$((attempt + 1))
    [ "$attempt" -ge 60 ] && fail "PostgreSQL never accepted connections"
    sleep 1
done
echo "PostgreSQL is accepting connections (read-only rootfs, 5 capabilities)."

step "Installing the three least-privilege roles"
# Before the migrations, not after: the ownership sweep in roles.sql exists for
# tables the superuser created before the roles did, and there are none here.
# Applying it first means sretab_migrate is the role that runs every
# CREATE TABLE from the start — the post-cutover steady state — and every table
# it creates picks up sretab_app's and sretab_readonly's grants from
# ALTER DEFAULT PRIVILEGES rather than from the one-off GRANT ON ALL TABLES.
# That is the mechanism most likely to be silently misconfigured, because
# naming the wrong role in FOR ROLE applies without error and simply never
# fires, so the whole run below depends on it having worked.
apply_roles
for role in sretab_migrate sretab_app sretab_readonly; do
    attrs=$(psql_db --command \
        "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls \
         FROM pg_catalog.pg_roles WHERE rolname = '$role'")
    [ "$attrs" = "f" ] || fail "$role is not a plain login role: $attrs"
done
echo "  sretab_migrate, sretab_app, and sretab_readonly exist, none privileged"

step "Running migrations"
# The same command deploy/quadlet/sre-tab-migrate.container runs, as the role
# it will run as after the cutover.
"$ENGINE" run --rm --name sre-tab-migrate --network "$NET" \
    --env "DATABASE_URL=$MIGRATE_DATABASE_URL" \
    --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64M \
    --security-opt=no-new-privileges --cap-drop all \
    --user 10001:10001 --pids-limit 64 \
    "$IMAGE" \
    sh -ec 'for attempt in 1 2 3 4 5 6 7 8 9 10; do alembic upgrade head && exit 0; sleep 3; done; exit 1'

revision=$(psql_db --command "SELECT version_num FROM alembic_version" | tr -d ' ')
[ -n "$revision" ] || fail "alembic_version is empty after upgrade head"
tables=$(psql_db --command \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" | tr -d ' ')
echo "Schema at revision $revision with $tables tables in public."
[ "$tables" -ge 12 ] || fail "expected at least 12 tables, found $tables"

# Owned by the role that created them, which is what lets a future migration
# ALTER or DROP them: PostgreSQL has no GRANT-able ALTER or DROP on a table,
# only ownership. A table owned by anyone else here means the migration
# container connected as the wrong role.
foreign=$(psql_db --command \
    "SELECT count(*) FROM pg_catalog.pg_class c \
     JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace \
     WHERE n.nspname = 'public' AND c.relkind = 'r' \
       AND pg_catalog.pg_get_userbyid(c.relowner) <> 'sretab_migrate'" | tr -d ' ')
[ "$foreign" -eq 0 ] || fail "$foreign tables in public are not owned by sretab_migrate"
echo "  every table owned by sretab_migrate"

step "The roles can do what deploy/ROLES.md says, and nothing more"
# The assertions ROLES.md's "Verification" section made by hand. They live
# here so that widening a role by accident fails CI, rather than leaving that
# document quietly wrong.

# The mechanism the whole finding turns on: COPY ... TO PROGRAM executes a
# command on the database server, and a superuser may. PostgreSQL gates it
# behind membership of pg_execute_server_program, which roles.sql grants to
# nobody — so this is refused by default, and the assertion is that it stays
# that way for the DDL role as much as for the other two.
assert_refused "sretab_app COPY ... TO PROGRAM" 'pg_execute_server_program' \
    psql_as sretab_app "$APP_PASSWORD" \
    --command "COPY (SELECT 1) TO PROGRAM 'touch /tmp/smoke-escalation'"
assert_refused "sretab_migrate COPY ... TO PROGRAM" 'pg_execute_server_program' \
    psql_as sretab_migrate "$MIGRATE_PASSWORD" \
    --command "COPY (SELECT 1) TO PROGRAM 'touch /tmp/smoke-escalation'"
assert_refused "sretab_readonly COPY ... TO PROGRAM" 'pg_execute_server_program' \
    psql_as sretab_readonly "$READONLY_PASSWORD" \
    --command "COPY (SELECT 1) TO PROGRAM 'touch /tmp/smoke-escalation'"

assert_refused "sretab_app CREATE TABLE" 'permission denied for schema public' \
    psql_as sretab_app "$APP_PASSWORD" \
    --command "CREATE TABLE smoke_app_ddl (id int)"
assert_refused "sretab_readonly INSERT" 'permission denied for table sources' \
    psql_as sretab_readonly "$READONLY_PASSWORD" \
    --command "INSERT INTO sources (slug, name, feed_url, website_url) \
               VALUES ('smoke', 'smoke', 'https://example.com/feed', 'https://example.com/')"
# DELETE specifically, and not only because it completes the set. Two units
# run the application image on a timer with nobody watching: the session
# sweep, whose entire job is a DELETE on `sessions`, and the hourly status
# check, which is two SELECTs. Which of them gets which credential is the
# unit-file check's business, above; this is the other half — that the two
# roles are genuinely different, so that check has something to protect.
# Widen sretab_readonly to make the swap survivable and this goes red.
# Asserted on `sessions` because that is the table the confusion is about.
assert_refused "sretab_readonly DELETE" 'permission denied for table sessions' \
    psql_as sretab_readonly "$READONLY_PASSWORD" \
    --command "DELETE FROM sessions"

# The other half: a table sretab_migrate creates is usable by the other two
# immediately, with no GRANT anywhere in this block. That is
# ALTER DEFAULT PRIVILEGES FOR ROLE sretab_migrate firing — the piece most
# likely to be silently misconfigured, because naming the wrong role there
# applies without error and then never does anything.
defaults_hint="ALTER DEFAULT PRIVILEGES FOR ROLE sretab_migrate is not firing"
psql_as sretab_migrate "$MIGRATE_PASSWORD" \
    --command "CREATE TABLE smoke_defaults (id serial primary key, note text)" >/dev/null \
    || fail "sretab_migrate cannot CREATE TABLE, which is the one thing it is for"
psql_as sretab_app "$APP_PASSWORD" \
    --command "INSERT INTO smoke_defaults (note) VALUES ('written-by-app')" >/dev/null \
    || fail "sretab_app cannot INSERT into a table sretab_migrate just created: $defaults_hint"
read_back=$(psql_as sretab_readonly "$READONLY_PASSWORD" \
    --command "SELECT note FROM smoke_defaults WHERE id = 1" | tr -d ' ')
[ "$read_back" = "written-by-app" ] \
    || fail "sretab_readonly read '$read_back' from a table sretab_migrate just created: $defaults_hint"
# The sequence behind the SERIAL key, not only the table: USAGE is what makes
# the implicit nextval() on INSERT work above, and SELECT is what pg_dump
# needs from sretab_readonly to emit a setval() for it.
seq_value=$(psql_as sretab_readonly "$READONLY_PASSWORD" \
    --command "SELECT last_value FROM smoke_defaults_id_seq" | tr -d ' ')
[ "$seq_value" = "1" ] \
    || fail "sretab_readonly cannot read a new sequence's value ('$seq_value'): $defaults_hint"
psql_as sretab_app "$APP_PASSWORD" \
    --command "UPDATE smoke_defaults SET note = 'updated' WHERE id = 1" >/dev/null \
    || fail "sretab_app cannot UPDATE a table sretab_migrate just created: $defaults_hint"
psql_as sretab_app "$APP_PASSWORD" \
    --command "DELETE FROM smoke_defaults WHERE id = 1" >/dev/null \
    || fail "sretab_app cannot DELETE from a table sretab_migrate just created: $defaults_hint"
psql_as sretab_migrate "$MIGRATE_PASSWORD" \
    --command "DROP TABLE smoke_defaults" >/dev/null \
    || fail "sretab_migrate cannot DROP a table it owns"
echo "  a new table from sretab_migrate is writable by sretab_app and readable by sretab_readonly"

step "Publishing frontend assets"
"$ENGINE" run --rm --network "$NET" \
    --volume "$ASSETS_VOL:/srv/www:rw" \
    --read-only --tmpfs /tmp:rw,nosuid,nodev,size=16M \
    --security-opt=no-new-privileges --cap-drop all \
    --user 10001:10001 --pids-limit 64 \
    "$IMAGE" \
    sh -ec 'find /srv/www -mindepth 1 -delete; cp -a /opt/sre-tab/web/. /srv/www/'

start_app() {
    # Flags mirror deploy/quadlet/sre-tab.container, no-new-privileges included
    # by its absence — see the note in that unit.
    "$ENGINE" rm --force sre-tab-app >/dev/null 2>&1 || true
    "$ENGINE" run --detach --name sre-tab-app --network "$NET" \
        --env "DATABASE_URL=$APP_DATABASE_URL" \
        --env "SESSION_SECRET=smoke-only-not-a-secret" \
        --env "APP_BASE_URL=http://127.0.0.1:$PORT" \
        --env "LOG_JSON=true" \
        --env "DOCS_ENABLED=false" \
        --env "SOURCE_REFRESH_ENABLED=$1" \
        --read-only --cap-drop all \
        --tmpfs /tmp:rw,nosuid,nodev,size=64M \
        --user 10001:10001 --pids-limit 256 \
        "$IMAGE" >/dev/null
}

# Refresh enabled, deliberately, and while the catalogue is still empty:
# the scheduler starts, takes its leader strategy from the live PostgreSQL
# dialect, and finds nothing due, so no feed is fetched. That is what makes
# the readiness assertion below meaningful without any outbound network.
step "Starting the application with the scheduler enabled"
start_app true

step "Starting Caddy in front of it"
"$ENGINE" run --detach --name sre-tab-web --network "$NET" \
    --volume "$repo_root/deploy/Caddyfile:/etc/caddy/Caddyfile:ro" \
    --volume "$ASSETS_VOL:/srv/www:ro" \
    --publish "127.0.0.1:$PORT:8080" \
    --read-only \
    --tmpfs /data:rw,nosuid,nodev,mode=1777 \
    --tmpfs /config:rw,nosuid,nodev,mode=1777 \
    --security-opt=no-new-privileges \
    --cap-drop all --cap-add NET_BIND_SERVICE \
    --user 65532:65532 --pids-limit 128 \
    "$CADDY_IMAGE" \
    caddy run --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null

step "Health check through the proxy"
attempt=0
until curl --fail --silent "http://127.0.0.1:$PORT/api/v1/healthz" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    [ "$attempt" -ge 60 ] && fail "/api/v1/healthz never returned 200 through Caddy"
    sleep 1
done
health=$(curl --silent "http://127.0.0.1:$PORT/api/v1/healthz")
printf '%s\n' "$health"

# The endpoint has to name its probes, not merely answer 200: a 503 that
# does not say which dependency is unhappy costs an operator the whole
# diagnosis. Criterion 6's "passes health checks" is this, not a status code.
printf '%s' "$health" | grep -q '"live":true' || fail "healthz does not report liveness"
printf '%s' "$health" | grep -q '"ready":true' || fail "healthz does not report readiness"
printf '%s' "$health" | grep -q '"database"' || fail "no database readiness probe"
printf '%s' "$health" | grep -q '"scheduler"' \
    || fail "no scheduler readiness probe: install_scheduler is not wired into create_app"

# The advisory-lock strategy is chosen from the live dialect, so seeing it
# here is the deployed configuration proving what unit tests can only assert
# about a fake engine.
printf '%s' "$health" | grep -q 'postgres-advisory' \
    || fail "the scheduler did not select the PostgreSQL advisory lock"
echo "  liveness, database readiness, and a running scheduler on postgres-advisory"

step "Restarting with the scheduler disabled for the rest of the run"
# Everything below seeds a catalogue of real feed URLs, and a smoke test has
# no business fetching them.
start_app false
attempt=0
until curl --fail --silent "http://127.0.0.1:$PORT/api/v1/healthz" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    [ "$attempt" -ge 60 ] && fail "the app did not come back with refresh disabled"
    sleep 1
done
curl --silent "http://127.0.0.1:$PORT/api/v1/healthz" | grep -q 'disabled' \
    || fail "the scheduler probe does not report the disabled posture"
echo "  scheduler probe reports ready-and-disabled"

step "Seeding the catalogue with the operator CLI"
"$ENGINE" exec sre-tab-app sre-tab seed
"$ENGINE" exec sre-tab-app sre-tab sources list
"$ENGINE" exec sre-tab-app sre-tab status

seeded=$(psql_db --command "SELECT count(*) FROM sources" | tr -d ' ')
[ "$seeded" -eq 7 ] || fail "expected 7 seeded sources, found $seeded"
topics=$(psql_db --command "SELECT count(*) FROM topics" | tr -d ' ')
[ "$topics" -eq 11 ] || fail "expected 11 seeded topics, found $topics"
# Every seeded source must carry default topics, or its items would be
# invisible under an explicit ?topics= filter.
untopiced=$(psql_db --command \
    "SELECT count(*) FROM sources s WHERE NOT EXISTS \
     (SELECT 1 FROM source_topics t WHERE t.source_id = s.id)" | tr -d ' ')
[ "$untopiced" -eq 0 ] || fail "$untopiced seeded sources have no default topics"

# The four slugs app/services/preferences.py defaults new users to. A
# mismatch here empties every new user's source selection, silently.
defaults=$(psql_db --command \
    "SELECT count(*) FROM sources \
     WHERE slug IN ('hacker-news','lobsters','dev-to','lwn')" | tr -d ' ')
[ "$defaults" -eq 4 ] || fail "the default source slugs are not all seeded"

# Idempotent: an operator re-running it after an upgrade must not double
# anything or undo a local change.
"$ENGINE" exec sre-tab-app sre-tab seed
reseeded=$(psql_db --command "SELECT count(*) FROM sources" | tr -d ' ')
[ "$reseeded" -eq 7 ] || fail "re-seeding changed the source count to $reseeded"
echo "  7 sources, 11 topics, all topiced, seed is idempotent"

step "Session sweep, as the application role"
# deploy/quadlet/sre-tab-prune-sessions.container, run the way it runs: the
# application image, the DML role, a read-only rootfs and no capabilities. It
# is a DELETE on one table, so it shares sretab_app rather than getting DDL
# rights it has no use for. Nothing has signed in here, so the sweep is empty
# — the assertion is on what it says, because an empty sweep and a sweep that
# never reached the database both exit zero.
prune_output=$("$ENGINE" run --rm --name sre-tab-prune-sessions --network "$NET" \
    --env "DATABASE_URL=$APP_DATABASE_URL" \
    --read-only --tmpfs /tmp:rw,nosuid,nodev,size=16M \
    --security-opt=no-new-privileges --cap-drop all \
    --user 10001:10001 --pids-limit 64 \
    "$IMAGE" sre-tab sessions prune 2>&1)
printf '%s\n' "$prune_output"
printf '%s' "$prune_output" | grep -q 'no dead sessions' \
    || fail "the session sweep did not report an empty sweep: $prune_output"
echo "  sre-tab sessions prune ran as sretab_app and swept nothing"

step "Source health check, as the read-only role"
# deploy/quadlet/sre-tab-status.container, run the way it runs: the same
# application image as the sweep above, the unit's own command, and the
# read-only role — the one application unit that needs no write access at all,
# because `sre-tab status` is two SELECTs and never commits. The unit-file
# check at the top of this run says which secret the unit names; this says the
# role behind that secret is actually sufficient, which is the half a missing
# SELECT grant would break.
run_status() {
    "$ENGINE" run --rm --name sre-tab-status --network "$NET" \
        --env "DATABASE_URL=$READONLY_DATABASE_URL" \
        --read-only --tmpfs /tmp:rw,nosuid,nodev,size=16M \
        --security-opt=no-new-privileges --cap-drop all \
        --user 10001:10001 --pids-limit 64 \
        "$IMAGE" sre-tab status --failures-over 3 2>&1
}
status_output=$(run_status) \
    || fail "sre-tab status exited non-zero on a healthy catalogue: $status_output"
printf '%s\n' "$status_output"
printf '%s' "$status_output" | grep -q 'hacker-news' \
    || fail "the status check read no catalogue at all: $status_output"

# ...and the exit code the timer turns into an alert, which is the half that
# would otherwise never be exercised: a `sre-tab status` that always exited
# zero would pass everything above. The row is planted as sretab_app, the role
# the scheduler writes it as, and takes one source four consecutive failures
# deep — over the unit's `--failures-over 3`, which is strictly over.
psql_as sretab_app "$APP_PASSWORD" --command \
    "INSERT INTO source_status (source_id, last_fetched_at, last_success_at, \
         last_error_class, last_error_detail, consecutive_failures) \
     SELECT id, now(), NULL, 'HTTPError', 'smoke-planted-failure', 4 \
     FROM sources WHERE slug = 'hacker-news'" >/dev/null
if failing_output=$(run_status); then
    fail "sre-tab status exited zero with a source four failures deep: $failing_output"
fi
printf '%s\n' "$failing_output"
printf '%s' "$failing_output" | grep -q 'smoke-planted-failure' \
    || fail "the status check did not name the failing source: $failing_output"
psql_as sretab_app "$APP_PASSWORD" --command "DELETE FROM source_status" >/dev/null
echo "  sre-tab status ran as sretab_readonly, and exits non-zero on a failing source"

step "Operator CLI refuses a target the fetcher would refuse"
# Acceptance criterion 5 at configuration time: no DNS, no socket, and the
# reason reaches the operator now rather than as a failing source later.
if "$ENGINE" exec sre-tab-app sre-tab sources add \
        --slug metadata --name Metadata \
        --feed-url http://169.254.169.254/latest/meta-data/ \
        --website-url https://example.com/ >/dev/null 2>&1; then
    fail "the CLI accepted a link-local plain-http feed URL"
fi
if "$ENGINE" exec sre-tab-app sre-tab sources add-medium-tag ../../etc/passwd \
        >/dev/null 2>&1; then
    fail "the CLI accepted a path-traversal Medium tag"
fi
still=$(psql_db --command "SELECT count(*) FROM sources" | tr -d ' ')
[ "$still" -eq 7 ] || fail "a refused source was written anyway"
echo "  hostile feed URL and hostile Medium tag both refused, nothing written"

step "Front-door behaviour"
check_status() {
    got=$(curl --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:$PORT$1")
    [ "$got" = "$2" ] || fail "$1 returned $got, expected $2"
    echo "  $1 -> $got"
}
check_status /            200
check_status /feed        200
check_status /theme-init.js 200
check_status /assets/definitely-not-here.js 404

# The document is only useful if the bundle it names actually resolves.
# Status codes alone would pass with an empty assets directory.
bundle=$(curl --silent "http://127.0.0.1:$PORT/" \
    | grep -o '/assets/[A-Za-z0-9._-]*\.js' | head -1)
[ -n "$bundle" ] || fail "index.html references no /assets/*.js bundle"
check_status "$bundle" 200
stylesheet=$(curl --silent "http://127.0.0.1:$PORT/" \
    | grep -o '/assets/[A-Za-z0-9._-]*\.css' | head -1)
[ -n "$stylesheet" ] || fail "index.html references no /assets/*.css stylesheet"
check_status "$stylesheet" 200

# The security headers the app cannot set on statically served files.
headers=$(curl --silent --dump-header - --output /dev/null "http://127.0.0.1:$PORT/")
for want in 'Content-Security-Policy' 'X-Content-Type-Options' 'X-Frame-Options' \
            'Referrer-Policy' 'Cross-Origin-Opener-Policy' 'Permissions-Policy'; do
    printf '%s' "$headers" | grep -qi "^$want:" || fail "$want missing on the SPA document"
done
printf '%s' "$headers" | grep -qi '^Cache-Control: no-store' \
    || fail "index.html is not no-store"
echo "  security headers and no-store present on /"

# ...and that the proxied API keeps the app's own headers rather than Caddy's.
api_headers=$(curl --silent --dump-header - --output /dev/null "http://127.0.0.1:$PORT/api/v1/healthz")
printf '%s' "$api_headers" | grep -qi '^Content-Security-Policy:' \
    || fail "the app's CSP did not survive the proxy path"
printf '%s' "$api_headers" | grep -qi '^Cache-Control: no-store' \
    && fail "Caddy leaked its static Cache-Control onto a proxied response"
echo "  app-set headers survive the proxy, static headers do not leak onto it"

step "Backup"
# A row that only exists in the dump, so the restore proves data round-trips
# rather than merely rebuilding an empty schema.
#
# Created by sretab_migrate and written by sretab_app, the two roles that
# would create and write it in production — not by the superuser. A table the
# superuser creates after roles.sql has run carries no grants for the other
# three at all (default privileges are FOR ROLE sretab_migrate), so the
# read-only backup below would fail on it, which is the correct behaviour and
# would be a misleading way to fail this test.
psql_as sretab_migrate "$MIGRATE_PASSWORD" \
    --command "CREATE TABLE smoke_marker (id int primary key, note text)" >/dev/null
psql_as sretab_app "$APP_PASSWORD" \
    --command "INSERT INTO smoke_marker VALUES (1, 'present-before-backup')" >/dev/null

# As sretab_readonly: the backup unit's post-cutover credential. This is also
# the assertion that the role's sequence grants are right — pg_dump emits a
# setval() per sequence and fails outright without SELECT on them, so a
# tables-only read role produces no backup rather than a subtly wrong one.
"$ENGINE" run --rm --network "$NET" \
    --volume "$backups:/backups:rw" \
    --volume "$repo_root/deploy/scripts/backup.sh:/usr/local/bin/sre-tab-backup:ro" \
    --env PGHOST=sre-tab-db --env PGUSER=sretab_readonly --env PGDATABASE=sretab \
    --env "PGPASSWORD=$READONLY_PASSWORD" \
    --env BACKUP_DIR=/backups --env BACKUP_KEEP_DAYS=14 \
    --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64M \
    --security-opt=no-new-privileges --cap-drop all \
    --user 999:999 --pids-limit 64 \
    "$PG_IMAGE" /usr/local/bin/sre-tab-backup

dump=$(find "$backups" -maxdepth 1 -name 'sretab-*.dump' | head -1)
[ -n "$dump" ] || fail "backup.sh produced no dump"
[ -f "$dump.sha256" ] || fail "backup.sh produced no checksum sidecar"

step "Destroying the data the restore must bring back"
# Dropped by its owner, which is the only role that can: no GRANT confers
# DROP on a table.
psql_as sretab_migrate "$MIGRATE_PASSWORD" --command "DROP TABLE smoke_marker" >/dev/null
psql_db --command "SELECT to_regclass('public.smoke_marker') IS NULL" | grep -q t \
    || fail "smoke_marker survived the drop"
echo "  smoke_marker dropped"

step "Restore"
# The operator-facing script, unmodified, on the path an operator would take —
# and with the split credential it now takes by default: PGPASSWORD is the
# superuser's, used only for DROP/CREATE DATABASE, while SRE_TAB_RESTORE_URL
# carries sretab_migrate's, which is what pg_restore itself connects as. On
# the deployment host those come from podman secrets instead; here they are
# environment variables, which is the documented --engine docker path.
PGPASSWORD="$DB_PASSWORD" SRE_TAB_RESTORE_URL="$MIGRATE_DATABASE_URL" \
    "$repo_root/deploy/scripts/restore.sh" \
    --engine "$ENGINE" --image "$PG_IMAGE" --network "$NET" \
    --no-systemd --yes "$dump"

step "Verifying the restore"
note=$(psql_db --command "SELECT note FROM smoke_marker WHERE id = 1" | tr -d ' ')
[ "$note" = "present-before-backup" ] || fail "smoke_marker did not come back: '$note'"
restored_revision=$(psql_db --command "SELECT version_num FROM alembic_version" | tr -d ' ')
[ "$restored_revision" = "$revision" ] || \
    fail "alembic_version changed across the restore: $revision -> $restored_revision"
echo "  smoke_marker restored, schema still at $restored_revision"

# The restore dropped the database, and every grant and default privilege in
# it went too. restore.sh re-applies roles.sql to the new database before
# pg_restore runs, so the roles come back with it; without that the
# application would return to a database it cannot read, and the health check
# below is not a strong enough test of that on its own — it reconnects lazily.
restored_owner=$(psql_db --command \
    "SELECT pg_catalog.pg_get_userbyid(relowner) FROM pg_catalog.pg_class \
     WHERE oid = 'public.smoke_marker'::regclass" | tr -d ' ')
[ "$restored_owner" = "sretab_migrate" ] \
    || fail "the restored smoke_marker is owned by $restored_owner, not sretab_migrate"
restored_note=$(psql_as sretab_app "$APP_PASSWORD" \
    --command "SELECT note FROM smoke_marker WHERE id = 1" | tr -d ' ')
[ "$restored_note" = "present-before-backup" ] \
    || fail "sretab_app cannot read the restored database: '$restored_note'"
assert_refused "sretab_app CREATE TABLE, after the restore" \
    'permission denied for schema public' \
    psql_as sretab_app "$APP_PASSWORD" --command "CREATE TABLE smoke_app_ddl (id int)"
echo "  the restored database is owned and privileged the same way it was"

step "Application recovers against the restored database"
attempt=0
until curl --fail --silent "http://127.0.0.1:$PORT/api/v1/healthz" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    [ "$attempt" -ge 60 ] && fail "the app did not become healthy again after the restore"
    sleep 1
done
echo "  /api/v1/healthz is 200 again"

step "Liveness and readiness are genuinely different answers"
# Last, because it takes the database away. A process that is alive but
# cannot reach its database must say so precisely: 503, live true, and the
# name of the probe that failed. Answering 200 would keep a useless replica
# in the load balancer; answering an unnamed 503 would cost the diagnosis.
"$ENGINE" stop sre-tab-db >/dev/null
degraded_status=$(curl --silent --output /tmp/sre-tab-smoke-health.json \
    --write-out '%{http_code}' "http://127.0.0.1:$PORT/api/v1/healthz")
degraded=$(cat /tmp/sre-tab-smoke-health.json)
rm -f /tmp/sre-tab-smoke-health.json
"$ENGINE" start sre-tab-db >/dev/null

[ "$degraded_status" = "503" ] || fail "healthz returned $degraded_status with no database"
printf '%s' "$degraded" | grep -q '"live":true' \
    || fail "the process reported itself not live when only the database was gone"
printf '%s' "$degraded" | grep -q '"ready":false' || fail "healthz stayed ready without a database"
printf '%s' "$degraded" | grep -q '"status":"degraded"' || fail "healthz did not report degraded"
printf '%s' "$degraded" | grep -q '"database":{"ok":false' \
    || fail "healthz did not name the database probe as the failure"
echo "  503 degraded, live true, database probe named"

attempt=0
until curl --fail --silent "http://127.0.0.1:$PORT/api/v1/healthz" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    [ "$attempt" -ge 60 ] && fail "the app did not recover after the database came back"
    sleep 1
done
echo "  and 200 again once the database is back"

printf '\nDeployment smoke test passed.\n'
