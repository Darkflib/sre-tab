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

PG_IMAGE=docker.io/library/postgres:18-trixie@sha256:06cad38a5d9f5d24b4d83d86def30795d5e4b757fedbf5281172b576dedcd941
CADDY_IMAGE=docker.io/library/caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648

NET=sre-tab-smoke
ASSETS_VOL=sre-tab-smoke-assets
DB_VOL=sre-tab-smoke-db
DB_PASSWORD=smoke-only-not-a-secret
DATABASE_URL="postgresql+psycopg://sretab:$DB_PASSWORD@sre-tab-db:5432/sretab"

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

step "Preparing $NET"
"$ENGINE" rm --force sre-tab-web sre-tab-app sre-tab-db >/dev/null 2>&1 || true
"$ENGINE" network rm --force "$NET" >/dev/null 2>&1 || true
"$ENGINE" volume rm --force "$ASSETS_VOL" "$DB_VOL" >/dev/null 2>&1 || true
"$ENGINE" network create "$NET" >/dev/null
"$ENGINE" volume create "$ASSETS_VOL" >/dev/null
"$ENGINE" volume create "$DB_VOL" >/dev/null

step "Starting PostgreSQL on a fresh volume"
# Flags mirror deploy/quadlet/sre-tab-db.container.
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
    --security-opt=no-new-privileges \
    --cap-drop all \
    --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
    --cap-add SETGID --cap-add SETUID \
    --pids-limit 256 \
    "$PG_IMAGE" >/dev/null

attempt=0
until "$ENGINE" exec sre-tab-db pg_isready --quiet --username=sretab --dbname=sretab; do
    attempt=$((attempt + 1))
    [ "$attempt" -ge 60 ] && fail "PostgreSQL never accepted connections"
    sleep 1
done
echo "PostgreSQL is accepting connections (read-only rootfs, 5 capabilities)."

step "Running migrations"
# The same command deploy/quadlet/sre-tab-migrate.container runs.
"$ENGINE" run --rm --name sre-tab-migrate --network "$NET" \
    --env "DATABASE_URL=$DATABASE_URL" \
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

step "Publishing frontend assets"
"$ENGINE" run --rm --network "$NET" \
    --volume "$ASSETS_VOL:/srv/www:rw" \
    --read-only --tmpfs /tmp:rw,nosuid,nodev,size=16M \
    --security-opt=no-new-privileges --cap-drop all \
    --user 10001:10001 --pids-limit 64 \
    "$IMAGE" \
    sh -ec 'find /srv/www -mindepth 1 -delete; cp -a /opt/sre-tab/web/. /srv/www/'

start_app() {
    "$ENGINE" rm --force sre-tab-app >/dev/null 2>&1 || true
    "$ENGINE" run --detach --name sre-tab-app --network "$NET" \
        --env "DATABASE_URL=$DATABASE_URL" \
        --env "SESSION_SECRET=smoke-only-not-a-secret" \
        --env "APP_BASE_URL=http://127.0.0.1:$PORT" \
        --env "LOG_JSON=true" \
        --env "DOCS_ENABLED=false" \
        --env "SOURCE_REFRESH_ENABLED=$1" \
        --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64M \
        --security-opt=no-new-privileges --cap-drop all \
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
psql_db --command \
    "CREATE TABLE smoke_marker (id int primary key, note text); \
     INSERT INTO smoke_marker VALUES (1, 'present-before-backup')" >/dev/null

"$ENGINE" run --rm --network "$NET" \
    --volume "$backups:/backups:rw" \
    --volume "$repo_root/deploy/scripts/backup.sh:/usr/local/bin/sre-tab-backup:ro" \
    --env PGHOST=sre-tab-db --env PGUSER=sretab --env PGDATABASE=sretab \
    --env "PGPASSWORD=$DB_PASSWORD" \
    --env BACKUP_DIR=/backups --env BACKUP_KEEP_DAYS=14 \
    --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64M \
    --security-opt=no-new-privileges --cap-drop all \
    --user 999:999 --pids-limit 64 \
    "$PG_IMAGE" /usr/local/bin/sre-tab-backup

dump=$(find "$backups" -maxdepth 1 -name 'sretab-*.dump' | head -1)
[ -n "$dump" ] || fail "backup.sh produced no dump"
[ -f "$dump.sha256" ] || fail "backup.sh produced no checksum sidecar"

step "Destroying the data the restore must bring back"
psql_db --command "DROP TABLE smoke_marker" >/dev/null
psql_db --command "SELECT to_regclass('public.smoke_marker') IS NULL" | grep -q t \
    || fail "smoke_marker survived the drop"
echo "  smoke_marker dropped"

step "Restore"
# The operator-facing script, unmodified, on the path an operator would take.
PGPASSWORD="$DB_PASSWORD" "$repo_root/deploy/scripts/restore.sh" \
    --engine "$ENGINE" --image "$PG_IMAGE" --network "$NET" \
    --no-systemd --yes "$dump"

step "Verifying the restore"
note=$(psql_db --command "SELECT note FROM smoke_marker WHERE id = 1" | tr -d ' ')
[ "$note" = "present-before-backup" ] || fail "smoke_marker did not come back: '$note'"
restored_revision=$(psql_db --command "SELECT version_num FROM alembic_version" | tr -d ' ')
[ "$restored_revision" = "$revision" ] || \
    fail "alembic_version changed across the restore: $revision -> $restored_revision"
echo "  smoke_marker restored, schema still at $restored_revision"

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
