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
#
# TWO CREDENTIALS, DELIBERATELY. `DROP DATABASE` and `CREATE DATABASE` are
# database-level administrative operations that none of the three roles in
# deploy/roles.sql holds, and none of them should: sretab_migrate owns the
# objects *inside* the sretab database, which PostgreSQL does not conflate
# with owning the database. So the recreate step keeps the administrative
# credential this script has always used (--user/--password-secret, the
# cluster superuser by default), and only the pg_restore step drops to
# sretab_migrate (--restore-user/--restore-url-secret) — which needs exactly
# the rights `alembic upgrade` needs and nothing beyond them.
#
# The alternative, granting sretab_migrate CREATEDB so one credential could
# do both, was considered and rejected. CREATEDB is cluster-wide and
# permanent: it would widen the role the migration unit runs unattended on
# every deploy, in exchange for convenience in a break-glass procedure a
# human runs with host root already in hand. The administrative credential
# has to keep existing regardless — deploy/ROLES.md's rollback path depends
# on sre-tab-database-url and sre-tab-postgres-password staying untouched —
# so keeping the split costs nothing new and grants nothing new.
#
# Pre-cutover, or on any host where the three roles were never installed,
# `--restore-user sretab` collapses this back to the single credential the
# script used before, and the check below says so by name rather than
# letting psql fail at authentication time.

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
  --user NAME         database superuser, used ONLY to drop and recreate the
                      database (default: sretab)
  --password-secret N podman secret holding that superuser's password
                      (default: sre-tab-postgres-password)
  --restore-user NAME role pg_restore itself connects as
                      (default: sretab_migrate)
  --restore-url-secret N
                      podman secret holding a DATABASE_URL for that role
                      (default: sre-tab-migrate-database-url). Ignored when
                      --restore-user matches --user, which restores the
                      single-credential behaviour for a host without the
                      roles installed.
  --no-systemd        do not stop or start systemd units; used by the smoke
                      test and on hosts where the app is not run by systemd
  --yes               do not prompt for confirmation
  -h, --help          this message

Both passwords are read from podman secrets by default. With --engine docker,
or when PGPASSWORD (superuser) and SRE_TAB_RESTORE_URL (restore role) are
already set in the environment, those values are used instead.
EOF
}

engine=podman
image=docker.io/library/postgres:18-trixie@sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280
network=systemd-sre-tab
db_host=sre-tab-db
database=sretab
db_user=sretab
password_secret=sre-tab-postgres-password
restore_user=sretab_migrate
restore_url_secret=sre-tab-migrate-database-url
use_systemd=true
assume_yes=false
dump=

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
roles_sql=$script_dir/../roles.sql

while [ "$#" -gt 0 ]; do
    case "$1" in
        --engine) engine=$2; shift 2 ;;
        --image) image=$2; shift 2 ;;
        --network) network=$2; shift 2 ;;
        --host) db_host=$2; shift 2 ;;
        --database) database=$2; shift 2 ;;
        --user) db_user=$2; shift 2 ;;
        --password-secret) password_secret=$2; shift 2 ;;
        --restore-user) restore_user=$2; shift 2 ;;
        --restore-url-secret) restore_url_secret=$2; shift 2 ;;
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

# The restore role's credential arrives as a whole DATABASE_URL rather than as
# a bare password, because that is the secret create-roles.sh actually mints
# for it: sre-tab-migrate-database-url exists so the migration unit can be
# handed one env var, and minting a second secret holding the same password in
# a different shape would be one more thing to keep in step. The password is
# taken out of it inside the client container (see run_restore_client), never
# here — a host-side parse would put it in this script's environment and in
# every child process it spawns.
restore_secret_args=""
restore_env_args=""
if [ "$restore_user" != "$db_user" ]; then
    if [ -n "${SRE_TAB_RESTORE_URL:-}" ]; then
        restore_env_args="--env SRE_TAB_RESTORE_URL"
    elif [ "$engine" = "podman" ]; then
        restore_secret_args="--secret $restore_url_secret,type=env,target=SRE_TAB_RESTORE_URL"
    else
        echo "error: set SRE_TAB_RESTORE_URL, or use --engine podman to read $restore_url_secret" >&2
        echo "       (or pass --restore-user $db_user to restore with one credential)" >&2
        exit 2
    fi
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

# Runs inside the client container, ahead of pg_restore. SRE_TAB_RESTORE_URL
# is `postgresql+psycopg://ROLE:PASSWORD@host:5432/db`, the shape
# create-roles.sh writes. Only the password is lifted out of it: libpq does
# not understand SQLAlchemy's `+psycopg` dialect suffix, and handing the whole
# URI to pg_restore --dbname would publish the password in the host's process
# table, which is the thing the podman secret exists to avoid.
#
# The role in the URL is checked against PGUSER rather than trusted, because
# the two come from different flags and disagreeing about them otherwise
# surfaces as `password authentication failed for user "sretab_migrate"` —
# which reads as a rotation problem and is not one.
# shellcheck disable=SC2016 # expanded by the container's shell, not by this one
restore_password_preamble='
creds=${SRE_TAB_RESTORE_URL#*://}
creds=${creds%%@*}
case "$creds" in
    *:*) ;;
    *) echo "error: the restore URL carries no password" >&2; exit 1 ;;
esac
if [ "${creds%%:*}" != "$PGUSER" ]; then
    echo "error: the restore URL is for role ${creds%%:*}, but --restore-user is $PGUSER" >&2
    exit 1
fi
PGPASSWORD=${creds#*:}
export PGPASSWORD
unset SRE_TAB_RESTORE_URL
exec "$@"
'

# Grants and default privileges live inside the database, and the restore
# drops the database: every table-level GRANT and every pg_default_acl row
# goes with it. This puts them back, on the administrative connection, and it
# runs twice.
#
# Before pg_restore, because that is what makes the restore possible at all:
# it gives sretab_migrate CREATE on schema public, without which the first
# CREATE TABLE in the dump is refused, and it re-establishes
# ALTER DEFAULT PRIVILEGES FOR ROLE sretab_migrate so that every table the
# restore creates arrives with sretab_app's DML grants and sretab_readonly's
# SELECT already attached.
#
# After it, because the pass before can only cover objects the restore role
# creates. Restoring with `--restore-user sretab` — the documented fallback —
# leaves every table owned by the superuser instead, which those default
# privileges do not reach, and the application would come back to a database
# it cannot read with nothing to say why. The second pass is roles.sql's
# ownership sweep and its GRANT ON ALL TABLES doing exactly the job they were
# written for. On the default path it is a genuine no-op, which is also worth
# having: every restore exercises the idempotency the install path relies on.
#
# No password is set or rotated: the three set_password_* flags are false,
# which is the path create-roles.sh takes on a re-run against roles that
# already exist. roles.sql raises if a role is missing, which is why the check
# above establishes that first. The unused password variables are still
# defined, because psql resolves a variable reference in a branch it is not
# taking.
apply_roles_sql() {
    {
        printf '\\set set_password_migrate false\n'
        printf '\\set migrate_password unused\n'
        printf '\\set set_password_app false\n'
        printf '\\set app_password unused\n'
        printf '\\set set_password_readonly false\n'
        printf '\\set readonly_password unused\n'
        cat "$roles_sql"
    } | run_client psql --quiet --no-psqlrc --set=ON_ERROR_STOP=1 \
        --dbname "$database"
}

run_restore_client() {
    if [ "$restore_user" = "$db_user" ]; then
        run_client "$@"
        return
    fi
    # shellcheck disable=SC2086 # deliberate word splitting of the arg groups
    "$engine" run --rm --interactive \
        --network "$network" \
        --volume "$dump_dir:/restore:ro" \
        --env "PGHOST=$db_host" \
        --env "PGUSER=$restore_user" \
        --env "PGDATABASE=$database" \
        $restore_env_args $restore_secret_args \
        --user 999:999 \
        --read-only \
        --security-opt=no-new-privileges \
        --cap-drop all \
        "$image" sh -ec "$restore_password_preamble" sre-tab-restore "$@"
}

echo "Verifying the dump is readable..."
run_client pg_restore --list "/restore/$dump_name" >/dev/null
echo "Dump parses cleanly."

# Deliberately not piped through `tr` to tidy the output: a pipeline's status
# is the last stage's, so a psql that could not connect at all would arrive
# here as an empty string and a zero exit, and be read below as "the role does
# not exist". Both callers assign it, so `set -e` stops on a failed query and
# shows psql's own message.
count_roles() {
    run_client psql --quiet --no-psqlrc --tuples-only --no-align \
        --set=ON_ERROR_STOP=1 --command "$1"
}

# Established here, on the administrative connection, before the confirmation
# prompt and long before DROP DATABASE. A restore that discovers the restore
# role does not exist only once the database is gone has already destroyed the
# thing it was about to fail on, and `FATAL: password authentication failed`
# from pg_restore is not a message that names the actual problem.
if [ "$restore_user" != "$db_user" ]; then
    present=$(count_roles \
        "SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname = '$restore_user'")
    if [ "$present" != 1 ]; then
        cat >&2 <<EOF
error: role $restore_user does not exist in this cluster, so nothing can
       restore as it. Nothing has been dropped.

       Install the three least-privilege roles first:

           sudo deploy/scripts/create-roles.sh

       or, on a host that has not been cut over to them yet, restore with the
       single administrative credential the way this script used to:

           deploy/scripts/restore.sh --restore-user $db_user ...

       deploy/ROLES.md has the reasoning for the split.
EOF
        exit 1
    fi

    # Existing is not the same as usable, and the difference is the whole
    # window this script is careful about. The check above proves the cluster
    # knows the role; it proves nothing about whether the credential this run
    # was handed still authenticates as it. A rotated create-roles.sh password
    # against a stale sre-tab-migrate-database-url passes the existence test
    # and then fails at pg_restore — which is line 498, after DROP DATABASE,
    # with an empty database where the data used to be and the comment above
    # correctly observing that `password authentication failed` does not name
    # the actual problem.
    #
    # So the credential is exercised, not merely present: a real connection as
    # the restore role, on the same path pg_restore will take, before the
    # confirmation prompt and long before anything is dropped. Connecting to
    # the database that is about to be replaced is fine — this only asks
    # whether the server will accept the credential.
    echo "Checking the restore credential authenticates as $restore_user..."
    if ! run_restore_client psql --quiet --no-psqlrc --tuples-only --no-align \
        --set=ON_ERROR_STOP=1 --command 'SELECT 1' >/dev/null; then
        cat >&2 <<EOF
error: could not connect as $restore_user with the credential given.
       Nothing has been dropped.

       The role exists, so this is the credential rather than the role. If
       create-roles.sh has rotated it, the podman secret this run read is
       stale relative to the cluster:

           sudo deploy/scripts/create-roles.sh --rotate

       rewrites both together. deploy/ROLES.md covers rotation.
EOF
        exit 1
    fi
fi

# Whether roles.sql has anything to re-apply after the recreate, decided while
# the roles can still be seen. It is a separate question from the one above:
# `--restore-user sretab` on a host that HAS been cut over still needs the
# grants put back, or the application returns to a database it cannot read.
roles_installed=false
if [ -f "$roles_sql" ]; then
    installed=$(count_roles \
        "SELECT count(*) FROM pg_catalog.pg_roles \
         WHERE rolname IN ('sretab_migrate', 'sretab_app', 'sretab_readonly')")
    if [ "$installed" = 3 ]; then
        roles_installed=true
    fi
fi

cat <<EOF

About to restore:
  dump         $dump_dir/$dump_name
  into         $database on $db_host (network $network)
  engine       $engine
  recreate as  $db_user
  restore as   $restore_user

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

# The backup timer is not part of "stopping the application", and it is the
# one unit that must not fire during the window below. Between DROP DATABASE
# and pg_restore finishing, the database is empty and perfectly healthy, so a
# backup landing in there dumps nothing, passes backup.sh's own
# `pg_restore --list` validation, and gets promoted from .partial to a final
# dump with a sha256 sidecar and today's date on it. Retention is by age, so
# it then sits in the directory for fourteen days looking exactly like a
# good backup.
#
# An in-flight run has to go too, not just the schedule. That is safe to
# interrupt: backup.sh writes to a .partial and only mv's it into place after
# validating, and it sweeps stale .partial files on its next run.
backup_timer_was_active=false
database_restored=false

release_backup_timer() {
    [ "$backup_timer_was_active" = true ] || return 0

    if [ "$database_restored" = true ]; then
        # Persistent=true, so if the scheduled hour passed while the restore
        # was running this fires straight away — which is what we want: the
        # first backup after a restore should be of the restored database.
        #
        # Not `|| true`. A swallowed failure here reports a successful
        # restore over a host whose backup schedule this script switched off
        # and never switched back on, and nothing would notice until someone
        # looked for a dump that was never taken. This branch is reached from
        # an EXIT trap, so `exit` is how the status gets changed.
        if ! systemctl start sre-tab-backup.timer; then
            cat >&2 <<'FAILED'

ERROR: the database was restored, but sre-tab-backup.timer could not be
       started again — this host currently has no backup schedule. Start it
       by hand and check why:

           systemctl start sre-tab-backup.timer
           systemctl status sre-tab-backup.timer

FAILED
            exit 1
        fi
        return 0
    fi

    # Deliberately left stopped. Re-arming on a restore that did not finish
    # would hand the next backup an empty or half-restored database — the
    # exact state the pause exists to keep it away from — and the result
    # would look like an ordinary dump, because it is a valid one. Silence
    # would be the other way to get this wrong, so it is said loudly rather
    # than left for the operator to notice in a fortnight.
    cat >&2 <<'WARNING'

WARNING: the restore did not complete, so sre-tab-backup.timer has been left
         stopped on purpose — backing up the database in its current state
         could overwrite good dumps with a bad one. Once the database is
         sound again, re-enable it:

           systemctl start sre-tab-backup.timer

WARNING
}

# A trap on INT or TERM runs the handler and then *carries on from where the
# signal landed*; it does not exit. Handling the signals with the same
# function as EXIT would therefore release the timer and then continue into
# DROP DATABASE with the schedule live again, which is worse than never
# having paused it. Disarm, clean up, re-raise: the re-raise is what gives
# the caller the conventional 128+signal status.
on_signal() {
    trap - EXIT "$1"
    release_backup_timer
    kill -s "$1" $$
}

if [ "$use_systemd" = true ]; then
    if systemctl is-active --quiet sre-tab-backup.timer; then
        backup_timer_was_active=true
    fi
    trap release_backup_timer EXIT
    trap 'on_signal INT' INT
    trap 'on_signal TERM' TERM
    echo "Pausing the backup timer so it cannot dump the empty database..."
    systemctl stop sre-tab-backup.timer sre-tab-backup.service

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
# Owned by the administrative role, not by the restore role. Making
# sretab_migrate the database owner would hand it DROP DATABASE on this
# database permanently — the same widening --restore-user exists to avoid,
# arrived at from the other direction.
run_client psql --quiet --no-psqlrc --set=ON_ERROR_STOP=1 \
    --command "CREATE DATABASE \"$database\" OWNER \"$db_user\""

if [ "$roles_installed" = true ]; then
    echo "Re-applying deploy/roles.sql to the new database..."
    apply_roles_sql
fi

# --single-transaction with --exit-on-error: the database is either fully
# restored or left empty for a second attempt. A half-restored schema that
# the app then migrates on top of is the failure mode worth designing out.
#
# --no-owner matters more now than it did. A dump taken before the cutover
# names the superuser as every table's owner, and sretab_migrate cannot
# ALTER ... OWNER TO a role it is not a member of, so honouring the dump's
# ownership would fail the whole single transaction. Without it, every object
# simply lands owned by whoever ran the restore — sretab_migrate — which is
# the ownership the three roles are built around, arrived at by construction
# rather than by a statement in the dump.
echo "Restoring as $restore_user..."
run_restore_client pg_restore \
    --dbname "$database" \
    --no-owner --no-privileges \
    --exit-on-error --single-transaction \
    "/restore/$dump_name"

if [ "$roles_installed" = true ]; then
    echo "Settling ownership and grants over the restored objects..."
    apply_roles_sql
fi

echo "Verifying..."
run_client psql --quiet --no-psqlrc --tuples-only --no-align \
    --dbname "$database" \
    --command "SELECT 'tables=' || count(*) FROM information_schema.tables WHERE table_schema = 'public'"
run_client psql --quiet --no-psqlrc --tuples-only --no-align \
    --dbname "$database" \
    --command "SELECT 'alembic_version=' || version_num FROM alembic_version"

# Past this line the database is restored and verified, so the backup timer
# can safely be released. Set here rather than at the end of the script: the
# application failing to come back up afterwards is a problem, but it is not
# a reason to keep the database unbacked.
database_restored=true

if [ "$use_systemd" = true ]; then
    echo "Starting the application..."
    systemctl start sre-tab-migrate.service
    systemctl start sre-tab.service

    # Which port the front door is on, asked of the front door.
    #
    # This used to be the literal 8080, which is only the default — a host
    # that has moved it with SRE_TAB_WEB_PORT would poll a port nothing
    # serves and end a successful restore by declaring the application
    # unhealthy. Caddy is untouched by a restore and is still running here,
    # so it can simply be asked.
    #
    # Asked of the container rather than read out of /etc/sre-tab/install.env
    # on purpose: that file is the *next* install's intention, and this loop
    # has to poll the port that is published *now*. An operator who edited it
    # and has not re-run the installer would otherwise send this check to the
    # wrong port at the one moment it is being relied on.
    web_port=$("$engine" port sre-tab-web 8080/tcp 2>/dev/null \
        | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p' | head -1)
    if [ -z "$web_port" ]; then
        # Not silently defaulted. A missing answer here means the front door
        # is not running, which is worth saying out loud before spending two
        # minutes discovering it against a guess.
        echo "warning: could not ask sre-tab-web which port it publishes;" >&2
        echo "         assuming the default 8080. If sre-tab-web.service is" >&2
        echo "         down, the check below will fail for that reason." >&2
        web_port=8080
    fi

    echo "Waiting for the health check on 127.0.0.1:$web_port..."
    attempt=0
    until curl --fail --silent --show-error \
        "http://127.0.0.1:$web_port/api/v1/healthz" >/dev/null; do
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
