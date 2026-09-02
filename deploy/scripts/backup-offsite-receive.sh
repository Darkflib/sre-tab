#!/bin/sh
#
# The far end of the ssh off-host copy. This does NOT run on the application
# host: it is installed on the machine receiving the dumps, and it is the only
# thing the sending host's key can run there.
#
#     command="/home/sre-tab-offsite/bin/backup-offsite-receive.sh",restrict \
#       ssh-ed25519 AAAA... sre-tab off-host backup
#
# The forced command means the sending host cannot ask for a shell, cannot ask
# for a different command, and cannot ask for anything not in the four verbs
# below. `restrict` removes port, agent, and X11 forwarding, the pty, and
# ~/.ssh/rc; ssh sets SSH_ORIGINAL_COMMAND to whatever was requested and runs
# this instead.
#
# THE SENDING HOST HAS NO VERB THAT DELETES, AND THAT IS THE DESIGN. A machine
# running a live database is the machine an attacker reaches first, and a
# backup target it can erase is not disaster recovery -- it is a second thing
# to lose in the same incident. So retention here is this host's decision,
# taken by this script, and only ever after a new dump has been verified on
# this host's own disk. Nothing the sender says can widen it, shorten it, or
# name a file for it.
#
# `put` refuses to overwrite a name that is already published, which is the
# ssh analogue of the Object Lock recommendation for S3: yesterday's good dump
# cannot be replaced with today's garbage by anything holding this key.
#
# Configuration is two variables. Defaults put the dumps under the receiving
# account's own home, so the far end needs no root at all -- an unprivileged
# account with this script in it is a complete installation. Override them in
# /etc/sre-tab-offsite-receive.conf if the dumps belong on a separate volume.

set -eu

# Dumps are readable only by this account. The sending host writes them 0600
# and there is no reason for them to relax on arrival.
umask 077

: "${HOME:=/nonexistent}"
RECEIVE_DIR="$HOME/sre-tab-offsite"
RECEIVE_KEEP_DAYS=14

if [ -r /etc/sre-tab-offsite-receive.conf ]; then
    # shellcheck source=/dev/null
    . /etc/sre-tab-offsite-receive.conf
fi

deny() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Parse and validate the request
# ---------------------------------------------------------------------------

request=${SSH_ORIGINAL_COMMAND:-}
[ -n "$request" ] || deny "no command; this key is restricted to the off-host backup receiver"

# Deliberate word splitting: the request is three space-separated fields and
# every one of them is validated below before it reaches a filesystem call.
# shellcheck disable=SC2086
set -- $request
verb=${1:-}
claimed_dir=${2:-}
name=${3:-}

case "$verb" in
    stat|put|verify|list) : ;;
    *) deny "unknown verb" ;;
esac

# The sender names the directory it believes it is writing to, and this host
# compares it against its own configuration rather than obeying it. That makes
# the path in the operator's target URL an assertion that is checked, instead
# of documentation that can quietly go stale -- and it means a mistyped URL
# fails on the first run rather than filling an unexpected directory.
[ "$claimed_dir" = "$RECEIVE_DIR" ] || deny "this key writes to one directory only"

# The published naming scheme from deploy/scripts/backup.sh, and nothing else:
# <database>-<UTC stamp>.dump, optionally with the .sha256 sidecar suffix.
# Anchored, with an explicit character class rather than a shell glob, so a
# name containing a slash, a leading dot, or .. cannot match. Nothing an
# operator has parked in this directory can be named by the sender, which is
# the same rule backup.sh's retention pass follows locally.
valid_name() {
    printf '%s' "$1" | grep -Eq '^[a-z0-9_]+-[0-9]{8}T[0-9]{6}Z\.dump(\.sha256)?$'
}

if [ "$verb" != list ]; then
    [ -n "$name" ] || deny "no file named"
    valid_name "$name" || deny "name does not match the published backup scheme"
fi

mkdir -p "$RECEIVE_DIR"
cd "$RECEIVE_DIR"

# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------

case "$verb" in

stat)
    # Presence and size only. The sender uses it to skip a transfer it has
    # already made; it is not evidence about the contents, and the sender does
    # not treat it as any -- `verify` is what answers that question.
    if [ -f "$name" ]; then
        printf 'present %s\n' "$(wc -c < "$name" | tr -d ' ')"
    else
        printf 'absent\n'
    fi
    ;;

put)
    [ ! -e "$name" ] || deny "$name already exists; this key cannot overwrite"

    partial=".$name.partial"
    cat > "$partial"

    case "$name" in
    *.dump)
        # Checked BEFORE publishing, against the sidecar that is already here
        # -- which is why the sender sends the sidecar first. A dump that
        # arrived truncated never gets a published name, so the append-only
        # rule above cannot trap a bad copy in place: the next run finds the
        # name still free and sends it again. The far end therefore never
        # holds an unverified file under a name anything would restore from.
        [ -f "$name.sha256" ] || { rm -f "$partial"; deny "no sidecar for $name yet"; }
        want=$(cut -d ' ' -f 1 < "$name.sha256")
        got=$(sha256sum "$partial" | cut -d ' ' -f 1)
        if [ "$want" != "$got" ]; then
            rm -f "$partial"
            deny "checksum mismatch on arrival: $got is not $want"
        fi
        ;;
    esac

    # Same directory, so the rename is atomic: nothing ever sees a partly
    # written dump under the published name.
    mv "$partial" "$name"
    printf 'stored %s\n' "$(wc -c < "$name" | tr -d ' ')"
    ;;

verify)
    # Re-read from disk. Not a cached answer from the `put` above, and not a
    # value this script was told -- the whole point of the exercise is that
    # the far end is asked what it is holding now.
    [ -f "$name" ] || deny "$name is not here"
    [ -f "$name.sha256" ] || deny "$name.sha256 is not here"
    sha256sum --check --status "$name.sha256" || deny "$name fails its own checksum here"
    printf 'verified %s\n' "$(sha256sum "$name" | cut -d ' ' -f 1)"

    # Retention, and only now. Deleting before the new dump had been verified
    # would mean a night on which the transfer failed was also the night the
    # oldest good copy was dropped. The database name comes from the file just
    # verified rather than from configuration, so this host needs to know
    # nothing about the sending host, and only that database's dumps are ever
    # considered.
    database=${name%-*}
    find . -maxdepth 1 -type f \
        \( -name "$database-*.dump" -o -name "$database-*.dump.sha256" \) \
        -mtime "+$RECEIVE_KEEP_DAYS" -print -delete \
        | sed 's|^\./|pruned |'
    # Leftovers from a transfer that died between `cat` and `mv`.
    find . -maxdepth 1 -type f -name ".$database-*.partial" -mtime +1 -delete
    ;;

list)
    # Name and size for every dump here, so the sending host's journal can
    # show what this host is actually holding. No timestamp column: the dump's
    # UTC stamp is already in its name, which is the same property that lets
    # the sender pick the newest dump by sorting names rather than by trusting
    # mtimes.
    for received in *.dump; do
        [ -f "$received" ] || continue
        printf '%s %s\n' "$received" "$(wc -c < "$received" | tr -d ' ')"
    done
    ;;

esac
