#!/bin/sh

set -eu

usage() {
    cat <<'EOF'
Usage: deploy/install.sh [--start]

Install the Developer News Dashboard Quadlets, the maintenance timers, the
alert template, the Caddy configuration, and the helper scripts.

  --start  Enable the timers and start the stack. Secrets must already
           exist (deploy/scripts/create-secrets.sh) and /etc/sre-tab/app.env
           must have been edited.

The installer is idempotent. Tracked files — Quadlets, the timers and the
alert template, the Caddyfile, backup.sh, and alert-dispatch.sh — are
replaced on every run; keep intentional changes in the repository rather
than editing the installed copies. Two files are the operator's and are
never overwritten: /etc/sre-tab/app.env, seeded once from
deploy/app.env.example, and /etc/sre-tab/alert.sh, which is never written
at all — alert.sh.example is installed beside it to be copied and edited.
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
# Run by sre-tab-alert@.service as root. 0755 rather than 0700 only because
# an operator wants to be able to run it by hand to test the alert path.
install -m 0755 "$script_dir/scripts/alert-dispatch.sh" "$config_dir/alert-dispatch.sh"
# NOT executable, and not named alert.sh: this is the template to copy, and
# a mode bit is the difference between a worked example and a transport that
# tries to mail ops@example.com.
install -m 0644 "$script_dir/scripts/alert.sh.example" "$config_dir/alert.sh.example"
install -m 0644 "$script_dir/quadlet/"*.network "$quadlet_dir/"
install -m 0644 "$script_dir/quadlet/"*.volume "$quadlet_dir/"
install -m 0644 "$script_dir/quadlet/"*.container "$quadlet_dir/"
install -m 0644 "$script_dir/systemd/"*.timer "$systemd_dir/"
# The alert template, which is a .service and would have been missed by the
# .timer glob above — a template that is never installed makes every
# OnFailure= pointing at it a no-op, and the failure it would have reported
# is the one this repository has no other way of reporting.
install -m 0644 "$script_dir/systemd/"*.service "$systemd_dir/"

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
    # The glob is *.timer and not *.service, which is what keeps
    # sre-tab-alert@.service out of this loop. A template does not want
    # enabling: OnFailure= instantiates it on demand, and it has no [Install]
    # section to enable anyway.
    #
    # Widening the glob would not have failed loudly, which is why it is
    # worth a comment. Measured on Debian 13 with systemd 257:
    # `systemctl enable sre-tab-alert@.service` prints "the unit files have
    # no installation config ... not meant to be enabled" and then EXITS
    # ZERO. Under `set -e` that reads as success, so the loop would have
    # gone on reporting a clean install while enabling nothing — a green
    # check that checked nothing, in a loop whose entire purpose is to make
    # sure a unit that was installed actually runs.
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

# The hourly source health check is installed and, with --start, enabled —
# and until this file exists it has nowhere to send anything. Said here, at
# install time, because the alternative is finding out at the moment an
# alert was supposed to arrive and did not, which is the exact failure the
# whole sre-tab-status/sre-tab-alert pair exists to remove.
#
# A warning rather than a refusal: a first install cannot be expected to
# have a transport written yet, and refusing would make the alert path a
# precondition for the deployment rather than part of it. It is not silent
# either way — alert-dispatch.sh exits non-zero when this file is missing,
# so an alert with nowhere to go lands in `systemctl --failed` with the
# report already in the journal.
if [ ! -x "$config_dir/alert.sh" ]; then
    cat >&2 <<WARNING

warning: $config_dir/alert.sh does not exist, or is not executable.
         sre-tab-status.timer checks hourly for a failing source and fires
         sre-tab-alert@sre-tab-status.service when one is found. That unit
         hands the report to $config_dir/alert.sh, and without it the alert
         reaches this host's journal and \`systemctl --failed\` and no
         further.

           cp $config_dir/alert.sh.example $config_dir/alert.sh
           \$EDITOR $config_dir/alert.sh    # mail via msmtp, or a webhook
           chmod 0755 $config_dir/alert.sh

         Then prove it, rather than assuming it:

           systemctl start sre-tab-alert@sre-tab-status.service
WARNING
fi
