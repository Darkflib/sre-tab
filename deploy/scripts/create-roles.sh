#!/usr/bin/env bash
#
# Install the three non-superuser PostgreSQL roles deploy/roles.sql defines
# (sretab_migrate, sretab_app, sretab_readonly) against the running database
# container, and mint the four podman secrets the Quadlet units consume:
# DATABASE_URL for the migration and application roles, and for the read-only
# one both a bare PGPASSWORD (pg_dump takes its credential that way) and a
# DATABASE_URL (`sre-tab status` takes one, like every other application
# unit) — see deploy/README.md, "Secrets".
#
#   deploy/scripts/create-roles.sh              # first install, or a re-run
#   deploy/scripts/create-roles.sh --rotate      # all three passwords, all
#                                                # four secrets, together
#
# THIS DOES NOT CHANGE WHAT ANYTHING CURRENTLY CONNECTS AS, even though every
# unit but the database now names one of these roles. It writes secrets and
# grants and restarts nothing, and a running container holds the credential it
# was started with — a podman secret is read once, at container start. So on a
# host mid-cutover this step is entirely reversible by doing nothing, and on a
# host already cut over a --rotate here must be followed by a restart or the
# next one will fail. deploy/ROLES.md has both procedures.
#
# It is deliberately not called from install.sh: creating the roles is a
# different decision from switching anything over to them, and keeping the two
# apart is what makes the rollback one `git revert`. It has to run against a
# database that is already up, which is why a first install starts
# sre-tab-db.service on its own before running this — install.sh --start says
# so by name if the secrets are missing.
#
# bash, not /bin/sh like its neighbours in this directory: `set -o pipefail`
# needs it, and this script pipes a generated psql script into `podman exec`
# rather than running one plain command per pipeline stage.
#
# Needs: podman, openssl. Runs the SQL through the postgres image's own
# psql, over the container's unix socket via `podman exec` — not over
# sre-tab.network — so no password is needed for the *connecting* superuser:
# the official image's default pg_hba.conf trusts local-socket connections
# unconditionally (confirmed against a real postgres:18; TCP connections
# still require scram-sha-256, which is what protects the three roles this
# script creates). That is also why this script takes no --password-secret
# option: unlike restore.sh's throwaway TCP client, there is no password to
# supply for this step.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: deploy/scripts/create-roles.sh [options]

Creates sretab_migrate, sretab_app, and sretab_readonly (deploy/roles.sql)
against the running database container, and writes the podman secrets the
Quadlet units take DATABASE_URL and PGPASSWORD from:

  sre-tab-migrate-database-url   DATABASE_URL for sretab_migrate
  sre-tab-app-database-url       DATABASE_URL for sretab_app
  sre-tab-readonly-password      PGPASSWORD for sretab_readonly
  sre-tab-readonly-database-url  DATABASE_URL for sretab_readonly

Four secrets for three roles: sretab_readonly's two hold the same password
in the two shapes its consumers want -- bare for pg_dump's PGPASSWORD, and a
whole URL for `sre-tab status`, which takes a DATABASE_URL like every other
application unit. They are written together and rotated together, because a
rotation that moved one and not the other would leave the backup and the
status check on different passwords with only one of them failing.

Options:
  --rotate          generate NEW passwords for all three roles, and rewrite
                     all four secrets. Four units read these secrets, and a
                     running container does not pick up a changed one --
                     follow this with a restart of sre-tab-migrate.service
                     and sre-tab.service. See the rotation note in
                     deploy/ROLES.md.
  --user NAME        superuser to connect as while installing (default: sretab)
  --database NAME     database to install into (default: sretab)
  --container NAME    the database container to run psql inside
                      (default: sre-tab-db)
  --host NAME         database host named in the generated connection URLs
                      -- the hostname the *other* containers reach it by on
                      sre-tab.network, not this script's own connection
                      (default: sre-tab-db)
  -h, --help          this message

Safe to re-run: with no flags, a role or secret that already exists is left
exactly as it is -- nothing is rotated, nothing is overwritten. Grants are
re-applied every time regardless (harmless: GRANT and ALTER DEFAULT
PRIVILEGES are no-ops when already in place), which is what makes it safe to
run again after a schema change lands new tables under the superuser by
accident, or to pick up a change to this script itself.

Refuses to run, rather than guess, if a role and its podman secret disagree
about whether they exist -- a secret with no role, or a role with no
secret. Neither pairing can be resolved by generating a new password for
one without the other; pass --rotate to resynchronise both.

Not called by install.sh. This is operator-invoked, deliberately -- creating
the roles is not the same decision as switching anything over to them, and
it needs a database that is already running, which on a first install means
`systemctl start sre-tab-db.service` before this and `install.sh --start`
after it.
EOF
}

rotate=false
db_user=sretab
db_name=sretab
container=sre-tab-db
db_host=sre-tab-db

while [ "$#" -gt 0 ]; do
    case "$1" in
        --rotate) rotate=true; shift ;;
        --user) db_user=$2; shift 2 ;;
        --database) db_name=$2; shift 2 ;;
        --container) container=$2; shift 2 ;;
        --host) db_host=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
done

command -v podman >/dev/null 2>&1 || { echo "error: podman not found" >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "error: openssl not found" >&2; exit 1; }

script_dir=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(CDPATH='' cd -- "$script_dir/../.." && pwd)
roles_sql="$repo_root/deploy/roles.sql"
[ -f "$roles_sql" ] || { echo "error: $roles_sql not found" >&2; exit 1; }

if ! podman container exists "$container"; then
    echo "error: no container named $container -- is the stack installed?" >&2
    exit 1
fi
if [ "$(podman inspect --format '{{.State.Running}}' "$container" 2>/dev/null)" != "true" ]; then
    echo "error: $container exists but is not running" >&2
    exit 1
fi

# base64url alphabet: no shell metacharacters, no SQL quoting to get wrong,
# and it never needs percent-encoding inside the connection URLs below --
# same reasoning and the same helper as create-secrets.sh.
random_urlsafe() {
    openssl rand -base64 "$1" | tr '+/' '-_' | tr -d '=\n'
}

run_psql() {
    # -i only: no -t, so this is never mistaken for an interactive session
    # and never waits on a tty that is not there in CI.
    podman exec -i "$container" \
        psql -v ON_ERROR_STOP=1 -U "$db_user" -d "$db_name"
}

query_role_exists() {
    podman exec "$container" \
        psql -v ON_ERROR_STOP=1 -U "$db_user" -d "$db_name" -tAc \
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '$1')"
}

echo "Checking current state..."
role_names=(migrate app readonly)
declare -A role_exists set_password new_password

role_secret_migrate=sre-tab-migrate-database-url
role_secret_app=sre-tab-app-database-url
role_secret_readonly=sre-tab-readonly-password
role_secret_readonly_url=sre-tab-readonly-database-url

# One password per role, but not one secret per role: sretab_readonly has two,
# holding that single password in the two shapes its consumers want. pg_dump
# takes a bare password through PGPASSWORD; `sre-tab status` takes a whole
# DATABASE_URL, like every other application unit. They are two spellings of
# one credential, so everything below treats them as a set -- a role whose
# secrets are only partly present has drifted exactly as much as one with no
# secret at all, and a rotation writes both or neither. Splitting them would
# leave the backup and the status check on different passwords, with only one
# of them failing and nothing saying why.
for r in "${role_names[@]}"; do
    case "$r" in
        migrate)  role_secrets=("$role_secret_migrate") ;;
        app)      role_secrets=("$role_secret_app") ;;
        readonly) role_secrets=("$role_secret_readonly" "$role_secret_readonly_url") ;;
    esac

    role_row=$(query_role_exists "sretab_$r")
    [ "$role_row" = "t" ] && role_exists[$r]=true || role_exists[$r]=false

    present=()
    missing=()
    for s in "${role_secrets[@]}"; do
        if podman secret exists "$s" 2>/dev/null; then
            present+=("$s")
        else
            missing+=("$s")
        fi
    done
    # So that a drift message reads as a sentence whether the role has one
    # secret or two.
    present_names=$(printf '%s and ' "${present[@]}"); present_names=${present_names% and }
    missing_names=$(printf '%s and ' "${missing[@]}"); missing_names=${missing_names% and }
    [ "${#present[@]}" -gt 1 ] && present_verb=exist || present_verb=exists
    [ "${#missing[@]}" -gt 1 ] && missing_verb="do not" || missing_verb="does not"

    if [ "${role_exists[$r]}" = false ] && [ "${#present[@]}" -gt 0 ] && [ "$rotate" = false ]; then
        cat >&2 <<EOF
error: $present_names already $present_verb, but role sretab_$r does not.
       The secret and the database have drifted apart -- generating a new
       password for one without the other would leave them disagreeing
       again. Re-run with --rotate to regenerate the secret and (re)create
       the role together.
EOF
        exit 1
    fi

    if [ "${role_exists[$r]}" = true ] && [ "${#missing[@]}" -gt 0 ] && [ "$rotate" = false ]; then
        cat >&2 <<EOF
error: role sretab_$r already exists, but $missing_names $missing_verb.
       The role and the secret have drifted apart -- writing a new secret
       without also rotating the role's password would leave them
       disagreeing again. Re-run with --rotate to regenerate the role's
       password and the secret together.
EOF
        exit 1
    fi

    if [ "${role_exists[$r]}" = false ] || [ "$rotate" = true ]; then
        set_password[$r]=true
        new_password[$r]=$(random_urlsafe 36)
    else
        set_password[$r]=false
        new_password[$r]=unused
    fi
done

echo "  sretab_migrate:  $([ "${role_exists[migrate]}" = true ] && echo exists || echo missing)$([ "${set_password[migrate]}" = true ] && echo ', password will be set' || true)"
echo "  sretab_app:      $([ "${role_exists[app]}" = true ] && echo exists || echo missing)$([ "${set_password[app]}" = true ] && echo ', password will be set' || true)"
echo "  sretab_readonly: $([ "${role_exists[readonly]}" = true ] && echo exists || echo missing)$([ "${set_password[readonly]}" = true ] && echo ', password will be set' || true)"

# Secrets are written before the SQL runs: if podman refuses the secret
# (disk full, no permission), nothing in the database has changed yet.
if [ "${set_password[migrate]}" = true ]; then
    printf 'postgresql+psycopg://sretab_migrate:%s@%s:5432/%s' \
        "${new_password[migrate]}" "$db_host" "$db_name" \
        | podman secret create --replace "$role_secret_migrate" - >/dev/null
    echo "  wrote secret $role_secret_migrate"
fi
if [ "${set_password[app]}" = true ]; then
    printf 'postgresql+psycopg://sretab_app:%s@%s:5432/%s' \
        "${new_password[app]}" "$db_host" "$db_name" \
        | podman secret create --replace "$role_secret_app" - >/dev/null
    echo "  wrote secret $role_secret_app"
fi
if [ "${set_password[readonly]}" = true ]; then
    printf '%s' "${new_password[readonly]}" \
        | podman secret create --replace "$role_secret_readonly" - >/dev/null
    echo "  wrote secret $role_secret_readonly"
    # The same password again, in the shape an application unit takes it.
    # From the same variable rather than a second call to random_urlsafe:
    # these are two spellings of one credential, and a second generation here
    # would produce two, of which the database would accept only the last one
    # written -- silently, until the backup or the status check next ran.
    printf 'postgresql+psycopg://sretab_readonly:%s@%s:5432/%s' \
        "${new_password[readonly]}" "$db_host" "$db_name" \
        | podman secret create --replace "$role_secret_readonly_url" - >/dev/null
    echo "  wrote secret $role_secret_readonly_url"
fi

echo "Applying deploy/roles.sql..."
{
    printf '\\set set_password_migrate %s\n' "${set_password[migrate]}"
    printf '\\set migrate_password %s\n' "${new_password[migrate]}"
    printf '\\set set_password_app %s\n' "${set_password[app]}"
    printf '\\set app_password %s\n' "${new_password[app]}"
    printf '\\set set_password_readonly %s\n' "${set_password[readonly]}"
    printf '\\set readonly_password %s\n' "${new_password[readonly]}"
    cat "$roles_sql"
} | run_psql

cat <<EOF

Done. The roles exist and their grants are current. Nothing that is already
running has changed -- a container holds the podman secret it started with,
so this took effect for the next start and not for the current one. Verify:

  podman exec $container psql -U $db_user -d $db_name -c '\du sretab_*'
  podman secret ls | grep sre-tab-

  # which roles are actually connected right now. pg_backend_pid() excludes
  # this psql, which connects as $db_user and would otherwise count itself.
  podman exec $container psql -U $db_user -d $db_name -c \\
    "SELECT usename, count(*) FROM pg_stat_activity WHERE datname = '$db_name' AND pid <> pg_backend_pid() GROUP BY usename"

On a host that has not been cut over yet, that last query still answers
"$db_user" and the ordered procedure to change it is in deploy/README.md,
under "Cutting a running deployment over to the roles". deploy/ROLES.md has
the reasoning, the verification record, and the rollback.
EOF
