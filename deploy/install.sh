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
    # Checked before the secrets because it is the one precondition here that
    # no script in this repository can create, and because it is the only one
    # whose absence does not announce itself. aardvark-dns is a *Recommends*
    # of Debian's podman package rather than a dependency, so a host built
    # with --no-install-recommends has podman, has netavark, and has no
    # container DNS at all. podman mentions it once, as a warning, and carries
    # on:
    #
    #   level=warning msg="aardvark-dns binary not found, container dns will not be enabled"
    #
    # Every container then starts normally and none of them can resolve
    # another by name, which is the only way anything here reaches anything
    # else: five units on sre-tab.network connect to the host `sre-tab-db`,
    # and the Caddyfile's upstream is `sre-tab-app:8000`. Measured on a fresh
    # Debian 13 host with podman 5.4.2, the first symptom is a psycopg "could
    # not translate host name" traceback out of sre-tab-migrate.service —
    # which reads like a database that is down, and is a host that was
    # installed thinly.
    #
    # Asked of podman rather than looked for on PATH, where it never is:
    # Debian installs the binary under /usr/lib/podman, other distributions
    # under /usr/libexec/podman, and containers.conf's helper_binaries_dir can
    # move it anywhere. podman reports the path it actually resolved, and
    # reports it empty when it resolved none.
    if dns_helper=$(podman info --format '{{.Host.NetworkBackendInfo.DNS.Path}}' 2>/dev/null); then
        if [ -z "$dns_helper" ]; then
            cat >&2 <<'EOF'
error: podman cannot find aardvark-dns, so containers on sre-tab.network
       will not resolve each other by name — and every hop in this stack is
       by name: five units reach the database as `sre-tab-db`, and Caddy
       reaches the application as `sre-tab-app`.

           sudo apt-get install aardvark-dns

       It is a Recommends of Debian's podman package, so a host installed
       with --no-install-recommends does not have it. Nothing else about
       such a host is wrong: podman starts every container, and they simply
       cannot see one another.
EOF
            exit 1
        fi
    else
        # Two things reach here: a podman whose `info` does not carry that
        # field, and no podman at all. Neither is grounds to refuse — the
        # check would be deciding on evidence it does not have — and neither
        # is grounds to say nothing, which is how a guard becomes a green
        # check that checked nothing.
        cat >&2 <<'EOF'
warning: could not ask podman where aardvark-dns is, so this installer has
         not checked for it. Without it containers do not resolve each other
         by name and nothing here reaches the database. Check by hand before
         concluding the stack is healthy:

             podman info --format '{{.Host.NetworkBackendInfo.DNS.Path}}'
EOF
    fi

    for secret in sre-tab-postgres-password sre-tab-database-url \
                  sre-tab-session-secret sre-tab-github-client-secret; do
        if ! podman secret exists "$secret" 2>/dev/null; then
            echo "error: podman secret '$secret' does not exist." >&2
            echo "       Run deploy/scripts/create-secrets.sh first." >&2
            exit 1
        fi
    done

    # The four least-privilege role secrets, checked separately because they
    # come from a different script and because a host can legitimately have
    # the four above and none of these — that is every host that has not yet
    # been cut over.
    #
    # Four secrets, three roles: sretab_readonly has two, holding one password
    # in the two shapes its consumers want — bare for the backup's PGPASSWORD,
    # and a whole URL for sre-tab-status.service, which takes a DATABASE_URL
    # like every other application unit. A host with only the bare password is
    # a host whose hourly health check will not start.
    #
    # sre-tab-database-url stays in the list above even though only the
    # rollback reads it now. It is the rollback: reverting the cutover commit
    # points four units back at it, and a host that has quietly lost it
    # cannot take that path.
    #
    # This check earns its place on a first install, where the ordering is
    # genuinely counter-intuitive. create-roles.sh talks to the running
    # database, so the roles cannot exist before the database does, and the
    # database is started by this script — so a fresh host has to start
    # sre-tab-db.service on its own, install the roles, and only then run
    # --start. Without this the sequence fails as three units refusing to
    # start with "no such secret", after the timers have been enabled, which
    # says what is missing but not what to do about it.
    for secret in sre-tab-migrate-database-url sre-tab-app-database-url \
                  sre-tab-readonly-password sre-tab-readonly-database-url; do
        if ! podman secret exists "$secret" 2>/dev/null; then
            cat >&2 <<EOF
error: podman secret '$secret' does not exist.
       The units connect as the three least-privilege roles, so they need
       the four secrets deploy/scripts/create-roles.sh writes. It installs them
       against the running database, which on a first deployment has to be
       started on its own first:

           sudo systemctl start sre-tab-db.service
           sudo deploy/scripts/create-roles.sh
           sudo deploy/install.sh --start

       deploy/ROLES.md has the whole picture, including the rollback.
EOF
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
         sre-tab-alert@sre-tab-status.service.service when one is found. That unit
         hands the report to $config_dir/alert.sh, and without it the alert
         reaches this host's journal and \`systemctl --failed\` and no
         further.

           cp $config_dir/alert.sh.example $config_dir/alert.sh
           \$EDITOR $config_dir/alert.sh    # mail via msmtp, or a webhook
           chmod 0755 $config_dir/alert.sh

         Then prove it, rather than assuming it:

           systemctl start sre-tab-alert@sre-tab-status.service.service
WARNING
fi
