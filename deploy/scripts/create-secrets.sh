#!/bin/sh
#
# Create the podman secrets the deployment needs.
#
#   sre-tab-postgres-password      POSTGRES_PASSWORD for the database
#   sre-tab-database-url           DATABASE_URL for the app and migrations
#   sre-tab-session-secret         SESSION_SECRET for CSRF/HMAC signing
#   sre-tab-github-client-secret   the OAuth app's client secret
#
# The database password appears in two of those, and a mismatch between them
# is the obvious way to spend an afternoon. This script generates it once and
# writes both, so they cannot disagree.
#
# The GitHub client secret is read from standard input and never appears as a
# command-line argument or an environment variable — the same discipline
# orbit-data applies to its Slack webhook, and for the same reason: argv is
# visible to every process on the host.
#
#   deploy/scripts/create-secrets.sh < /path/to/client-secret
#   printf '%s' 'ghs_...' | deploy/scripts/create-secrets.sh

set -eu

usage() {
    cat <<'EOF'
Usage: deploy/scripts/create-secrets.sh [options]

Reads the GitHub OAuth client secret from standard input.

Options:
  --rotate-db      generate a NEW database password. The database container
                   will not adopt it on its own: see deploy/README.md.
  --user NAME      database role (default: sretab)
  --database NAME  database name (default: sretab)
  --host NAME      database host on the container network
                   (default: sre-tab-db)
  -h, --help       this message

Existing secrets are replaced. Secrets are per-user under rootless podman and
system-wide under rootful podman; run this as the same user that runs the
quadlet units — root, for the reference deployment.
EOF
}

rotate_db=false
db_user=sretab
db_name=sretab
db_host=sre-tab-db

while [ "$#" -gt 0 ]; do
    case "$1" in
        --rotate-db) rotate_db=true; shift ;;
        --user) db_user=$2; shift 2 ;;
        --database) db_name=$2; shift 2 ;;
        --host) db_host=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
done

command -v podman >/dev/null 2>&1 || { echo "error: podman not found" >&2; exit 1; }

if [ -t 0 ]; then
    echo "error: the GitHub client secret must arrive on stdin, not a terminal" >&2
    echo "       deploy/scripts/create-secrets.sh < /path/to/client-secret" >&2
    exit 2
fi

github_client_secret=$(cat)
if [ -z "$github_client_secret" ]; then
    echo "error: empty GitHub client secret on stdin" >&2
    exit 1
fi

put_secret() {
    # --replace keeps this script idempotent. Value on stdin only.
    podman secret create --replace "$1" - >/dev/null
    echo "  $1"
}

# base64url alphabet, so the password can sit in a URL without any
# percent-encoding and DATABASE_URL stays copy-pasteable.
random_urlsafe() {
    openssl rand -base64 "$1" | tr '+/' '-_' | tr -d '=\n'
}

if podman secret exists sre-tab-postgres-password 2>/dev/null && [ "$rotate_db" = false ]; then
    echo "error: sre-tab-postgres-password already exists." >&2
    echo "       Re-running would generate a new password that the existing" >&2
    echo "       database does not have. Pass --rotate-db if that is the" >&2
    echo "       intent, and read the rotation note in deploy/README.md." >&2
    exit 1
fi

db_password=$(random_urlsafe 36)
session_secret=$(random_urlsafe 48)

echo "Creating secrets:"
printf '%s' "$db_password" | put_secret sre-tab-postgres-password
printf 'postgresql+psycopg://%s:%s@%s:5432/%s' \
    "$db_user" "$db_password" "$db_host" "$db_name" \
    | put_secret sre-tab-database-url
printf '%s' "$session_secret" | put_secret sre-tab-session-secret
printf '%s' "$github_client_secret" | put_secret sre-tab-github-client-secret

cat <<'EOF'

Done. Verify with:

  podman secret ls

Values are not printable back out of podman by design; if you need the
database password for a manual psql session, read it from the running
container's environment as root, or rotate it.
EOF
