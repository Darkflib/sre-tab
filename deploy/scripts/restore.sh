#!/bin/sh
#
# Restore the Developer News Dashboard database from a dump produced by
# deploy/scripts/backup.sh.
#
# This is the documented restore procedure AND the one CI exercises: the
# deployment smoke test (deploy/scripts/smoke.sh) calls this same script with
# --no-systemd against a throwaway database. A restore procedure that is only
# prose is a procedure nobody has run.
#
# THIS DESTROYS THE TARGET DATABASE. It drops it and rebuilds it from the
# dump; anything written since the dump is gone.

set -eu

usage() {
    cat <<'EOF'
Usage: deploy/scripts/restore.sh [options] <dump-file>

Restores a pg_dump custom-format dump into the running database container.

Options:
  --engine NAME       podman (default) or docker
  --image REF         postgres image to run the client from
                      (default: the pinned image from the backup quadlet)
  --network NAME      container network (default: systemd-sre-tab)
  --host NAME         database host on that network (default: sre-tab-db)
  --database NAME     database to restore into (default: sretab)
  --user NAME         database superuser (default: sretab)
  --password-secret N podman secret holding the password
                      (default: sre-tab-postgres-password)
  --no-systemd        do not stop or start systemd units; used by the smoke
                      test and on hosts where the app is not run by systemd
  --yes               do not prompt for confirmation
  -h, --help          this message

The password is read from a podman secret by default. With --engine docker,
or when PGPASSWORD is already set in the environment, that value is used
instead.
EOF
}

engine=podman
image=docker.io/library/postgres:18-trixie@sha256:06cad38a5d9f5d24b4d83d86def30795d5e4b757fedbf5281172b576dedcd941
network=systemd-sre-tab
db_host=sre-tab-db
database=sretab
db_user=sretab
password_secret=sre-tab-postgres-password
use_systemd=true
assume_yes=false
dump=

while [ "$#" -gt 0 ]; do
    case "$1" in
        --engine) engine=$2; shift 2 ;;
        --image) image=$2; shift 2 ;;
        --network) network=$2; shift 2 ;;
        --host) db_host=$2; shift 2 ;;
        --database) database=$2; shift 2 ;;
        --user) db_user=$2; shift 2 ;;
        --password-secret) password_secret=$2; shift 2 ;;
        --no-systemd) use_systemd=false; shift ;;
        --yes) assume_yes=true; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "error: unknown option $1" >&2; usage >&2; exit 2 ;;
        *)
            if [ -n "$dump" ]; then
                echo "error: more than one dump file given" >&2
                exit 2
            fi
            dump=$1
            shift
            ;;
    esac
done

if [ -z "$dump" ]; then
    usage >&2
    exit 2
fi

if [ ! -f "$dump" ]; then
    echo "error: no such dump file: $dump" >&2
    exit 1
fi

dump_dir=$(CDPATH='' cd -- "$(dirname -- "$dump")" && pwd)
dump_name=$(basename -- "$dump")

# Verify the checksum sidecar when backup.sh wrote one. A dump that has been
# truncated in transit off-host is worth catching before the target database
# is dropped, not after.
#
# Checked inside a container as uid 999, not on the host. backup.sh writes
# under `umask 077` as that uid, so the dump and its sidecar are 0600 and
# owned by it -- an operator running this script as themselves cannot read
# either. Doing it on the host worked only for root, and reported the
# resulting EACCES as "checksum mismatch", which points at data corruption
# when the real problem is the reader.
verify_checksum() {
    "$engine" run --rm \
        --volume "$dump_dir:/restore:ro" \
        --workdir /restore \
        --user 999:999 \
        --read-only \
        --network none \
        --security-opt=no-new-privileges \
        --cap-drop all \
        "$image" sha256sum --check --status "$dump_name.sha256"
}

if [ -f "$dump_dir/$dump_name.sha256" ]; then
    if verify_checksum; then
        echo "checksum OK: $dump_name"
    else
        echo "error: checksum mismatch for $dump_name" >&2
        exit 1
    fi
else
    echo "note: no .sha256 sidecar for $dump_name; skipping checksum check" >&2
fi

secret_args=""
if [ -n "${PGPASSWORD:-}" ]; then
    env_args="--env PGPASSWORD"
elif [ "$engine" = "podman" ]; then
    secret_args="--secret $password_secret,type=env,target=PGPASSWORD"
    env_args=""
else
    echo "error: set PGPASSWORD, or use --engine podman to read $password_secret" >&2
    exit 2
fi

run_client() {
    # shellcheck disable=SC2086 # deliberate word splitting of the arg groups
    "$engine" run --rm --interactive \
        --network "$network" \
        --volume "$dump_dir:/restore:ro" \
        --env "PGHOST=$db_host" \
        --env "PGUSER=$db_user" \
        --env "PGDATABASE=postgres" \
        $env_args $secret_args \
        --user 999:999 \
        --read-only \
        --security-opt=no-new-privileges \
        --cap-drop all \
        "$image" "$@"
}

echo "Verifying the dump is readable..."
run_client pg_restore --list "/restore/$dump_name" >/dev/null
echo "Dump parses cleanly."

cat <<EOF

About to restore:
  dump      $dump_dir/$dump_name
  into      $database on $db_host (network $network)
  engine    $engine

THIS DROPS $database AND EVERYTHING IN IT.
EOF

if [ "$assume_yes" = false ]; then
    printf 'Type the database name to continue: '
    read -r confirm
    if [ "$confirm" != "$database" ]; then
        echo "aborted" >&2
        exit 1
    fi
fi

if [ "$use_systemd" = true ]; then
    echo "Stopping the application so nothing writes during the restore..."
    systemctl stop sre-tab.service sre-tab-migrate.service
fi

# DROP ... WITH (FORCE) terminates any surviving backend rather than failing
# on "database is being accessed by other users". Stopping the app above is
# still the right thing to do; this covers the psql session an operator left
# open in another window.
echo "Recreating $database..."
run_client psql --quiet --no-psqlrc --set=ON_ERROR_STOP=1 \
    --command "DROP DATABASE IF EXISTS \"$database\" WITH (FORCE)"
run_client psql --quiet --no-psqlrc --set=ON_ERROR_STOP=1 \
    --command "CREATE DATABASE \"$database\" OWNER \"$db_user\""

# --single-transaction with --exit-on-error: the database is either fully
# restored or left empty for a second attempt. A half-restored schema that
# the app then migrates on top of is the failure mode worth designing out.
echo "Restoring..."
run_client pg_restore \
    --dbname "$database" \
    --no-owner --no-privileges \
    --exit-on-error --single-transaction \
    "/restore/$dump_name"

echo "Verifying..."
run_client psql --quiet --no-psqlrc --tuples-only --no-align \
    --dbname "$database" \
    --command "SELECT 'tables=' || count(*) FROM information_schema.tables WHERE table_schema = 'public'"
run_client psql --quiet --no-psqlrc --tuples-only --no-align \
    --dbname "$database" \
    --command "SELECT 'alembic_version=' || version_num FROM alembic_version"

if [ "$use_systemd" = true ]; then
    echo "Starting the application..."
    systemctl start sre-tab-migrate.service
    systemctl start sre-tab.service
    echo "Waiting for the health check..."
    attempt=0
    until curl --fail --silent --show-error http://127.0.0.1:8080/api/v1/healthz >/dev/null; do
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 30 ]; then
            echo "error: the application did not become healthy after the restore" >&2
            exit 1
        fi
        sleep 2
    done
    echo "Healthy."
fi

echo "Restore complete."
