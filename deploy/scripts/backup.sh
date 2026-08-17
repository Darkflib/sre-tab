#!/bin/sh
#
# Daily PostgreSQL backup. Runs inside a postgres image, on the container
# network, as uid 999 — see deploy/quadlet/sre-tab-backup.container.
#
# Connection details come from the standard libpq environment variables
# (PGHOST, PGUSER, PGDATABASE, PGPASSWORD); PGPASSWORD arrives as a podman
# secret, so it is absent from the unit file and from `podman inspect`.
#
# Installed to /etc/sre-tab/backup.sh and bind-mounted read-only.

set -eu

# A dump is a complete copy of every user record, every session-linked row,
# and every bookmark in the instance. It is created 0600, not left to the
# default 0644, because the directory mode must not be the only thing standing
# between it and another account: /srv/sre-tab/backups is group 999, and on
# Debian 13 gid 999 is `systemd-journal` — a group operators genuinely do add
# people to so they can read the journal. Anyone in it could otherwise read
# every dump on the host. The sha256 sidecars are written under the same mask.
umask 077

: "${BACKUP_DIR:=/backups}"
: "${BACKUP_KEEP_DAYS:=14}"
: "${PGDATABASE:?PGDATABASE must be set}"

if [ ! -d "$BACKUP_DIR" ]; then
    echo "error: $BACKUP_DIR does not exist" >&2
    exit 1
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
final="$BACKUP_DIR/$PGDATABASE-$stamp.dump"
partial="$BACKUP_DIR/.$PGDATABASE-$stamp.dump.partial"

# Custom format, not plain SQL: it is compressed, it is what pg_restore
# consumes, and `pg_restore --list` can verify it without a database.
pg_dump --format=custom --compress=zstd --file="$partial"

# Verify before publishing. A truncated or corrupt dump fails to list, and
# finding that out now is the difference between a failed backup job — which
# is visible — and a backup that only reveals itself as useless during a
# restore.
pg_restore --list "$partial" >/dev/null

# Same filesystem, so the rename is atomic: no reader, and no retention pass,
# ever sees a half-written dump under the published name.
mv "$partial" "$final"

( cd "$BACKUP_DIR" && sha256sum "$(basename "$final")" > "$(basename "$final").sha256" )

bytes=$(wc -c < "$final")
echo "backup complete: $final (${bytes} bytes)"

# Retention. Only files matching the published naming scheme are considered,
# so nothing an operator has parked in this directory is at risk.
find "$BACKUP_DIR" -maxdepth 1 -type f -name "$PGDATABASE-*.dump" \
    -mtime "+$BACKUP_KEEP_DAYS" -print -delete
find "$BACKUP_DIR" -maxdepth 1 -type f -name "$PGDATABASE-*.dump.sha256" \
    -mtime "+$BACKUP_KEEP_DAYS" -print -delete
# Leftovers from a run that died between pg_dump and mv.
find "$BACKUP_DIR" -maxdepth 1 -type f -name ".$PGDATABASE-*.partial" \
    -mtime +1 -print -delete
