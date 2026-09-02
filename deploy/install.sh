#!/bin/sh

set -eu

usage() {
    cat <<'EOF'
Usage: deploy/install.sh [--start]

Install the Developer News Dashboard Quadlets, the maintenance timers, the
Caddy configuration, and the backup script.

  --start  Enable the timers and start the stack. Secrets must already
           exist (deploy/scripts/create-secrets.sh) and /etc/sre-tab/app.env
           must have been edited.

The installer is idempotent. Tracked files — Quadlets, the timers, the
Caddyfile, and backup.sh — are replaced on every run; keep intentional
changes in the repository rather than editing the installed copies.
/etc/sre-tab/app.env is the exception: it is seeded once from
deploy/app.env.example and never overwritten, because it is the operator's.
EOF
}

start_services=false
case "${1:-}" in
    "") ;;
    --start) start_services=true ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

if [ "$#" -gt 1 ]; then
    usage >&2
    exit 2
fi

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
install_root=${DESTDIR:-}

if [ -n "$install_root" ]; then
    if [ "$start_services" = true ]; then
        echo "error: --start cannot be used with DESTDIR" >&2
        exit 2
    fi
elif [ "$(id -u)" -ne 0 ]; then
    echo "error: run this installer as root" >&2
    exit 1
fi

quadlet_dir="$install_root/etc/containers/systemd"
systemd_dir="$install_root/etc/systemd/system"
config_dir="$install_root/etc/sre-tab"
backup_dir="$install_root/srv/sre-tab/backups"

install -d -m 0755 "$quadlet_dir" "$systemd_dir" "$config_dir"

# uid/gid 999 is `postgres` inside the pinned postgres image; the backup
# container runs as that user and writes here.
#
# 0700 rather than 0750, and this is not fussiness. Those ids mean `postgres`
# *in the image*; on the host they mean whatever the host says, and on Debian
# 13 gid 999 is `systemd-journal` — a group admins routinely add people to for
# journal access. A group-readable directory here would hand every one of them
# the dumps, which contain every user record in the instance. Nothing needs
# group access: the backup container writes as uid 999, and restore.sh runs as
# root. backup.sh sets `umask 077` as well, so neither layer is load-bearing
# alone.
install -d -m 0700 -o 999 -g 999 "$backup_dir" 2>/dev/null \
    || install -d -m 0700 "$backup_dir"

install -m 0644 "$script_dir/Caddyfile" "$config_dir/Caddyfile"
install -m 0755 "$script_dir/scripts/backup.sh" "$config_dir/backup.sh"
install -m 0755 "$script_dir/scripts/backup-offsite.sh" "$config_dir/backup-offsite.sh"
# Not run on this host. Staged here so an operator setting up the far end has
# it to copy across, and so an upgrade updates the copy they will send next.
install -m 0644 "$script_dir/scripts/backup-offsite-receive.sh" \
    "$config_dir/backup-offsite-receive.sh"
install -m 0644 "$script_dir/backup-offsite.env.example" \
    "$config_dir/backup-offsite.env.example"
install -m 0644 "$script_dir/quadlet/"*.network "$quadlet_dir/"
install -m 0644 "$script_dir/quadlet/"*.volume "$quadlet_dir/"
install -m 0644 "$script_dir/quadlet/"*.container "$quadlet_dir/"
install -m 0644 "$script_dir/systemd/"*.timer "$systemd_dir/"
install -m 0644 "$script_dir/systemd/"*.service "$systemd_dir/"

# Drop-ins for units Quadlet generates. systemd merges these from
# /etc/systemd/system/<unit>.d/ for a generated unit exactly as for an ordinary
# one, which is how sre-tab-backup.service gains its OnSuccess= without
# deploy/quadlet/sre-tab-backup.container being edited.
for dropin_dir in "$script_dir/systemd/"*.service.d; do
    [ -d "$dropin_dir" ] || continue
    install -d -m 0755 "$systemd_dir/$(basename "$dropin_dir")"
    install -m 0644 "$dropin_dir/"*.conf "$systemd_dir/$(basename "$dropin_dir")/"
done

# The operator's file. Seeded once, then left alone — an installer that
# overwrites it turns every upgrade into an outage.
if [ ! -f "$config_dir/app.env" ]; then
    install -m 0640 "$script_dir/app.env.example" "$config_dir/app.env"
    echo "Seeded $config_dir/app.env — edit it before starting the stack"
fi

if [ -n "$install_root" ]; then
    echo "Developer News Dashboard deployment files staged below $install_root"
    exit 0
fi

# The identity sre-tab-backup-offsite.service runs as. Created unconditionally,
# even where off-host copies are not configured: a shell-less system account
# with no home costs nothing, and making it conditional on
# /etc/sre-tab/backup-offsite.env would mean an operator who writes that file
# without re-running this installer gets their first off-host copy at 03:22
# with no user and no ACL, which is a trap with a twelve-hour fuse.
if ! getent passwd sre-tab-offsite >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin \
        --comment "sre-tab off-host backup copier" sre-tab-offsite
    echo "Created the sre-tab-offsite system user"
fi

# How that user is allowed to read the dumps, and the only way it is.
#
# $backup_dir is 0700 and owned by uid 999 for the reason spelled out above, so
# an unprivileged account cannot read a thing in it. The alternatives were to
# loosen the mode, which reintroduces the gid 999 / systemd-journal problem, or
# to give the unit CAP_DAC_READ_SEARCH, which grants it read access to every
# file on the host — /etc/shadow and the podman secrets included — in order to
# read two. A POSIX ACL grants exactly one extra reader on exactly this tree,
# is enforced by the kernel's own DAC, and leaves CapabilityBoundingSet= empty.
#
# The default ACL is the half that is easy to miss. New dumps are created by
# the backup container under `umask 077`; when a directory carries a default
# ACL the umask is ignored and the default supplies the mode instead, so each
# night's dump comes out `-rw-r-----+` with `user::rw-, group::---, other::---`
# and a single named entry for sre-tab-offsite. Measured on Debian 13 / ext4:
# a member of gid 999 still gets EACCES on both the directory and the files.
# `ls -ld` shows `drwxr-x---+`, which looks like group access and is not — the
# group bits of a file with an ACL are the mask, not `group::`.
#
# Re-applied on every run, deliberately: the `install -d -m 0700` above sets
# the mask to `---` and silently masks the grant out, so an installer re-run is
# how this breaks and an installer re-run is how it is fixed.
if command -v setfacl >/dev/null 2>&1; then
    setfacl -m u:sre-tab-offsite:rx "$backup_dir"
    setfacl -d -m u:sre-tab-offsite:r "$backup_dir"
else
    echo "warning: setfacl not found, so sre-tab-offsite cannot be granted" >&2
    echo "         read access to $backup_dir. Off-host copies will fail" >&2
    echo "         with a permission error. Install it and re-run:" >&2
    echo "           apt-get install acl && $0" >&2
fi

# The operator's own file, holding the S3 secret. Not created here — its
# existence is what switches the feature on — but its mode is worth one look.
if [ -f "$config_dir/backup-offsite.env" ]; then
    offsite_mode=$(stat -c %a "$config_dir/backup-offsite.env")
    # Leading 0 so the shell reads it as octal, then any group or other bit.
    if [ "$(( 0$offsite_mode & 077 ))" -ne 0 ]; then
        echo "warning: $config_dir/backup-offsite.env is mode $offsite_mode." >&2
        echo "         It carries the off-host S3 secret, and systemd reads it" >&2
        echo "         as root before dropping privilege, so nothing else needs" >&2
        echo "         to:  chmod 0600 $config_dir/backup-offsite.env" >&2
    fi
fi

systemctl daemon-reload

if [ "$start_services" = true ]; then
    for secret in sre-tab-postgres-password sre-tab-database-url \
                  sre-tab-session-secret sre-tab-github-client-secret; do
        if ! podman secret exists "$secret" 2>/dev/null; then
            echo "error: podman secret '$secret' does not exist." >&2
            echo "       Run deploy/scripts/create-secrets.sh first." >&2
            exit 1
        fi
    done

    # Quadlet services are transient generated units and cannot be enabled
    # with `systemctl enable`; their [Install] sections are applied by the
    # generator at boot and on daemon-reload, so starting them explicitly is
    # enough. The timers are native units and are enabled normally.
    #
    # Enumerated from deploy/systemd rather than listed by hand: the units are
    # installed by a glob a few lines up, so a timer added to the repository
    # and staged by that glob but forgotten here would be installed, never
    # enabled, and silently never run — which for a job that exists to stop a
    # table growing without bound is indistinguishable from working.
    for timer_path in "$script_dir/systemd/"*.timer; do
        systemctl enable --now "$(basename "$timer_path")"
    done

    # Clears a start-rate limit, nothing else. A unit that has been crash-
    # looping — sre-tab-web under an address collision, say — hits
    # StartLimitBurst and then refuses to start at all until its failed state
    # is reset, so an installer that did not do this could not repair the very
    # situation an operator runs it to repair.
    #
    # It is deliberately NOT here to tidy up after ordinary stops. Those used
    # to land in `systemctl --failed` because uvicorn exited 1 on every clean
    # shutdown; that is fixed at the source in deploy/quadlet/sre-tab.container,
    # so anything showing up in `systemctl --failed` now is real. Do not widen
    # this line to hide it.
    systemctl reset-failed sre-tab-db.service sre-tab-migrate.service \
        sre-tab-assets.service sre-tab.service sre-tab-web.service 2>/dev/null || true

    # Named explicitly and restarted in ONE transaction, so systemd resolves
    # the After= ordering across all six: network, database, migrations,
    # assets, application, front door. Restart rather than start so a re-run
    # adopts changed unit files and a newer image.
    #
    # The network unit is in the list because it is a RemainAfterExit oneshot:
    # once it has run it stays "active (exited)" whether or not the network it
    # created still exists, so an operator who removes the network — which is
    # the documented way to adopt a changed subnet or IPRange — would otherwise
    # find every container failing with "unable to find network with name or ID
    # systemd-sre-tab". Its ExecStart is `podman network create --ignore` and it
    # has no ExecStop, so restarting it re-creates a missing network and does
    # nothing at all to a healthy one.
    systemctl restart \
        sre-tab-network.service \
        sre-tab-db.service \
        sre-tab-migrate.service \
        sre-tab-assets.service \
        sre-tab.service \
        sre-tab-web.service
fi

echo "Developer News Dashboard deployment files installed"
if [ "$start_services" = false ]; then
    echo "Run $0 --start to enable the timers and start the stack"
fi
