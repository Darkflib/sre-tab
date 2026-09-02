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

One host-policy setting is read rather than installed:
/etc/sre-tab/install.env, which is never created here either. Set
SRE_TAB_WEB_PORT in it to publish the front door somewhere other than
127.0.0.1:8080, and re-run. Only the host side moves; the container still
listens on 8080.
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

# --- The published host port ----------------------------------------------
#
# Read and validated here, before anything is written, so that a bad value
# leaves the host exactly as it was rather than half-installed.
#
# Which host port the front door appears on is host policy, not a property of
# this application, and it therefore cannot live in the repository. 8080 is a
# collision waiting to happen on any host that does more than one thing, and
# on the reference deployment it collided: the operator edited `PublishPort=`
# in the tracked deploy/quadlet/sre-tab-web.container, which works, and which
# survives exactly until the next `git checkout`, `git stash`, or `git reset
# --hard` — and this installer copies that file over /etc on every run, so the
# edit had nowhere else it could live. This setting is where it lives instead.
#
# Only the HOST side moves. The container still listens on 8080: the
# Caddyfile's site block, the image's healthcheck, and the application's
# upstream all name it, and none of the three is an operator's business.
#
# The file is read as a table of values, not sourced. Every other .env under
# /etc/sre-tab is read by systemd as an `EnvironmentFile=`, which is not
# shell; sourcing this one would make it the single file in that directory
# whose contents run as root, and would make `SRE_TAB_WEB_PORT=$(…)` mean
# something. The last assignment wins, which is systemd's rule for a file of
# this shape.
#
# Read from $config_dir rather than from /etc/sre-tab literally, so that a
# DESTDIR stage reads the tree it is staging into and not the host's live
# configuration.
web_port=8080
web_port_set=false
install_env="$config_dir/install.env"

if [ -f "$install_env" ]; then
    # The `x` prefix distinguishes "assigned nothing" from "never mentioned".
    # sed prints nothing for the second and a bare `x` for the first, and the
    # two mean different things: an operator who wrote `SRE_TAB_WEB_PORT=`
    # got it wrong and should be told so, while one who wrote no such line
    # asked for the default and should be left alone.
    web_port_raw=$(sed -n 's/^[[:space:]]*SRE_TAB_WEB_PORT=/x/p' "$install_env" | tail -n 1)
    if [ -n "$web_port_raw" ]; then
        web_port_set=true
        web_port=${web_port_raw#x}
        # One surrounding pair of quotes, because systemd's EnvironmentFile
        # accepts them and fingers that have written a dozen of those files
        # will write them here too. Anything else is left exactly as found
        # and fails validation below with the value quoted back.
        case $web_port in
            '"'*'"')
                web_port=${web_port#'"'}
                web_port=${web_port%'"'}
                ;;
            "'"*"'")
                web_port=${web_port#"'"}
                web_port=${web_port%"'"}
                ;;
        esac
    fi
fi

if [ "$web_port_set" = true ]; then
    # Refused here rather than left to fail later, and the "later" is the
    # whole point. Quadlet generates a perfectly well-formed unit for
    # `--publish 127.0.0.1:70000:8080` and `podman-system-generator --dryrun`
    # exits 0 over it; podman refuses it at container start with `parsing host
    # port: port numbers must be between 1 and 65535 (inclusive), got 70000`,
    # which is during a restart, with the old container already gone. A value
    # this installer cannot make sense of is a value it should not install.
    web_port_ok=true
    case $web_port in
        # Empty, non-numeric, or leading zero. The last is in the list
        # because `08080` is digits and is not a port anybody means, and
        # because `test -ge` has no obligation to read it as decimal.
        '' | *[!0-9]* | 0*) web_port_ok=false ;;
    esac
    # Length before magnitude: `test -ge` on a thirty-digit string is
    # undefined rather than false, so the range check has to be handed
    # something that fits in a long first.
    if [ "$web_port_ok" = true ] && [ "${#web_port}" -gt 5 ]; then
        web_port_ok=false
    fi
    if [ "$web_port_ok" = true ] && [ "$web_port" -gt 65535 ]; then
        web_port_ok=false
    fi

    if [ "$web_port_ok" != true ]; then
        cat >&2 <<EOF
error: SRE_TAB_WEB_PORT in $install_env is '$web_port', which is not a port.

       It must be a whole number from 1 to 65535, with no leading zero, and
       it names the HOST side of the front door only — the container still
       listens on 8080 whatever this says.

       Nothing has been installed and nothing has been changed. Fix the
       file and run this again.
EOF
        exit 2
    fi

    # Warned about, deliberately not refused. Rootful podman may bind a
    # privileged port and this unit publishes on 127.0.0.1 only, so nothing
    # off-host can reach it whatever the number — there is no measurement
    # here that would support a refusal, and inventing one would refuse a
    # choice that works. It is still worth a second look, because below 1024
    # is where the host's own listeners are, including the TLS proxy that on
    # the documented topology forwards to this very port. That collision does
    # not appear until a restart, when podman cannot bind and
    # sre-tab-web.service fails.
    if [ "$web_port" -lt 1024 ]; then
        echo "warning: SRE_TAB_WEB_PORT=$web_port is a privileged port. It is" >&2
        echo "         accepted — rootful podman can bind it and the unit" >&2
        echo "         publishes on loopback — but below 1024 is where the" >&2
        echo "         host's own listeners live, the TLS proxy that forwards" >&2
        echo "         here included, and a collision only shows up at the" >&2
        echo "         next restart. Check it:  ss -lntp | grep :$web_port" >&2
    fi
fi

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
install -m 0755 "$script_dir/scripts/backup-offsite.sh" "$config_dir/backup-offsite.sh"
# Not run on this host. Staged here so an operator setting up the far end has
# it to copy across, and so an upgrade updates the copy they will send next.
install -m 0644 "$script_dir/scripts/backup-offsite-receive.sh" \
    "$config_dir/backup-offsite-receive.sh"
install -m 0644 "$script_dir/backup-offsite.env.example" \
    "$config_dir/backup-offsite.env.example"
# Staged like the two examples above and, like them, never turned into the
# real file. Its absence is what means "the defaults", so creating it here
# would replace an absent setting with a written-down one nobody chose.
install -m 0644 "$script_dir/install.env.example" "$config_dir/install.env.example"
install -m 0644 "$script_dir/quadlet/"*.network "$quadlet_dir/"
install -m 0644 "$script_dir/quadlet/"*.volume "$quadlet_dir/"
install -m 0644 "$script_dir/quadlet/"*.container "$quadlet_dir/"

# The published host port, expressed as a Quadlet drop-in rather than as an
# edit to the unit file installed on the line above.
#
# Quadlet merges `<unit>.container.d/*.conf` exactly as systemd merges a
# `.service.d` drop-in — the same mechanism this installer already uses a few
# lines down to give sre-tab-backup.service its `OnSuccess=`, one layer up.
# So sre-tab-web.container is installed byte for byte from the repository and
# the port arrives beside it, which is precisely the property the reference
# host's local edit destroyed. `diff` between deploy/quadlet and
# /etc/containers/systemd stays empty on a host that has moved its port.
#
# A .service.d drop-in cannot do this job, which is worth saying because it is
# the obvious first idea: PublishPort= ends up inside the generated
# `ExecStart=`, so overriding it there means restating podman's entire command
# line and re-deriving it after every podman upgrade.
#
# Nor can the unit carry a variable. Measured on Debian 13 / podman 5.4.2:
# Quadlet copies `PublishPort=127.0.0.1:${SRE_TAB_WEB_PORT}:8080` through to
# `--publish 127.0.0.1:${SRE_TAB_WEB_PORT}:8080` verbatim, expanding nothing.
# systemd *would* then expand it from an `EnvironmentFile=` in [Service], and
# on a throwaway unit built that way it did — but an empty assignment expands
# to `--publish 127.0.0.1::8080`, which podman accepts by publishing on a
# random ephemeral port: measured as `8080/tcp -> 127.0.0.1:37341`, with
# `systemctl start` exiting 0 and `systemctl --failed` empty. The unit is up,
# nothing complains, and the front door is somewhere nobody is looking. It
# would also put the value beyond this validation, since it is read at
# container start rather than here. A mechanism whose failure mode is silent
# is worse than the hardcoded line it replaces.
#
# The bare `PublishPort=` below is load-bearing and is easy to leave out.
# Quadlet honours systemd's list semantics, so an assignment ADDS and an empty
# assignment RESETS. Measured with `podman-system-generator --dryrun`: without
# the reset the generated ExecStart carries `--publish 127.0.0.1:8080:8080
# --publish 127.0.0.1:8081:8080` — the old port still open beside the new one,
# which is the half of "move the port" that is easy to miss because the new
# port does work.
web_dropin_dir="$quadlet_dir/sre-tab-web.container.d"
web_dropin="$web_dropin_dir/10-published-port.conf"
if [ "$web_port_set" = true ]; then
    install -d -m 0755 "$web_dropin_dir"
    web_dropin_tmp=$(mktemp "${TMPDIR:-/tmp}/sre-tab-port.XXXXXX")
    cat > "$web_dropin_tmp" <<EOF
# Generated by deploy/install.sh from SRE_TAB_WEB_PORT in
# /etc/sre-tab/install.env. Edit that file and re-run the installer; an edit
# here is overwritten on the next run.
#
# The empty PublishPort= resets the list the unit file set. Without it this
# would open a second port rather than move the one there is.
[Container]
PublishPort=
PublishPort=127.0.0.1:$web_port:8080
EOF
    install -m 0644 "$web_dropin_tmp" "$web_dropin"
    rm -f "$web_dropin_tmp"
    echo "Front door published on 127.0.0.1:$web_port (container port 8080)"
else
    # An absent setting means the shipped default, and that has to include
    # taking the port back from a host that used to have one. Removing the
    # file is what makes this setting reversible rather than one-way: comment
    # the line out, re-run, and the host is byte for byte what a host that
    # never had the file is.
    rm -f "$web_dropin"
    rmdir "$web_dropin_dir" 2>/dev/null || true
fi

install -m 0644 "$script_dir/systemd/"*.timer "$systemd_dir/"
# The alert template, which is a .service and would have been missed by the
# .timer glob above — a template that is never installed makes every
# OnFailure= pointing at it a no-op, and the failure it would have reported
# is the one this repository has no other way of reporting.
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
    # move it anywhere. podman reports what it actually resolved.
    #
    # The whole document and a substring, rather than a Go template naming
    # the field that holds the path. This started as the template, and the
    # template has a third state: one that names a field this podman does not
    # carry exits non-zero having reported nothing, which is not
    # distinguishable from the binary being absent. Every way of handling
    # that state is wrong — refusing decides on evidence it does not have,
    # warning and continuing is the green check that checks nothing — so the
    # right move was to stop producing it. `--format json` cannot fail that
    # way: either aardvark-dns is in the document or it is not, and no podman
    # at all is the empty document, which refuses like any other host that
    # cannot resolve a container name.
    if ! podman info --format json 2>/dev/null | grep -q aardvark-dns; then
        cat >&2 <<'EOF'
error: podman reports no aardvark-dns, so containers on sre-tab.network will
       not resolve each other by name — and every hop in this stack is by
       name: five units reach the database as `sre-tab-db`, and Caddy reaches
       the application as `sre-tab-app`.

           sudo apt-get install aardvark-dns

       It is a Recommends of Debian's podman package, so a host installed
       with --no-install-recommends does not have it. Nothing else about such
       a host is wrong: podman starts every container, and they simply cannot
       see one another. What podman says about it:

           podman info --format json | grep aardvark-dns
EOF
        exit 1
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
