#!/bin/sh
#
# Gather context about a failed unit and hand it to the operator's own alert
# transport. Run by sre-tab-alert@.service, which sre-tab-status.container
# points its OnFailure= at; installed to /etc/sre-tab/alert-dispatch.sh by
# deploy/install.sh.
#
#   /etc/sre-tab/alert-dispatch.sh sre-tab-status.service
#
# This half is the repository's. The other half — actually reaching a person
# — is /etc/sre-tab/alert.sh, which the operator writes and this repository
# never installs, because mail, a webhook, a pager, and an existing
# monitoring agent are all correct answers and choosing one here would put a
# transport dependency in a project that has deliberately few. Copy
# /etc/sre-tab/alert.sh.example and edit it.
#
# The seam, so an implementation can be written against it:
#
#   argv[1]   the failed unit's name, e.g. sre-tab-status.service
#   stdin     the whole report: unit, result, exit status, timestamps, and
#             the last 50 journal lines from that unit
#   env       SRE_TAB_ALERT_UNIT, SRE_TAB_ALERT_RESULT,
#             SRE_TAB_ALERT_STATUS, SRE_TAB_ALERT_HOST
#
# A transport that only needs the text can `cat` its stdin and ignore the
# rest. Its exit status is this script's exit status, so a webhook that
# refuses the POST lands in `systemctl --failed` instead of being believed.
#
# The report is written to stdout as well as piped, unconditionally, so it
# reaches the journal under sre-tab-alert@<unit>.service even when there is
# no transport at all. That is the floor this design refuses to go below: an
# alert with nowhere to go is still recorded, and still fails loudly.

set -eu

ALERT_SCRIPT=${SRE_TAB_ALERT_SCRIPT:-/etc/sre-tab/alert.sh}
JOURNAL_LINES=${SRE_TAB_ALERT_JOURNAL_LINES:-50}

unit=${1:-}
if [ -z "$unit" ]; then
    echo "error: no unit name given; this is run as: $0 <unit>" >&2
    exit 2
fi

host=$(hostname 2>/dev/null || echo unknown)

# `systemctl show` rather than `systemctl status`: it is stable, parseable,
# and exits zero for a unit in any state, including one that has just failed.
# --value keeps the output to the values in the order asked for.
properties=$(systemctl show "$unit" \
    --property=Result --property=ExecMainStatus \
    --property=InactiveEnterTimestamp --property=InvocationID \
    --value 2>/dev/null || true)

result=$(printf '%s\n' "$properties" | sed -n 1p)
status=$(printf '%s\n' "$properties" | sed -n 2p)
finished=$(printf '%s\n' "$properties" | sed -n 3p)
invocation=$(printf '%s\n' "$properties" | sed -n 4p)

# The journal for that unit is the alert body. sre-tab-status.container sets
# LogDriver=none precisely so that systemd's own capture of the container's
# stdout is the single copy, which means the status table and the per-source
# error lines are here rather than somewhere podman keeps them.
journal=$(journalctl --unit "$unit" --lines "$JOURNAL_LINES" --no-pager 2>&1 \
    || echo '(journalctl produced nothing)')

report=$(
    cat <<REPORT
sre-tab: $unit failed on $host

unit:       $unit
host:       $host
result:     ${result:-unknown}
exit:       ${status:-unknown}
finished:   ${finished:-unknown}
invocation: ${invocation:-unknown}

Last $JOURNAL_LINES journal lines from $unit:

$journal
REPORT
)

printf '%s\n' "$report"

if [ ! -x "$ALERT_SCRIPT" ]; then
    # The loud half of the absent-transport case. Exiting non-zero puts
    # sre-tab-alert@<unit>.service into `systemctl --failed`, which is the
    # host's existing catch-all, and the report above is already in the
    # journal. Both matter: a silent alert path is the specific defect this
    # unit was written to remove, so its own misconfiguration must not be
    # the one thing that fails quietly.
    # The example is named relative to the transport rather than hardcoded,
    # so the instructions stay correct when SRE_TAB_ALERT_SCRIPT moves the
    # seam — which is how this script is tested.
    cat >&2 <<MISSING
error: $unit failed and there is no alert transport to tell anyone.
       $ALERT_SCRIPT does not exist, or is not executable.

       The report above reached this host's journal and nothing else. To
       wire up a real transport:

         cp ${ALERT_SCRIPT%/*}/alert.sh.example $ALERT_SCRIPT
         \$EDITOR $ALERT_SCRIPT
         chmod 0755 $ALERT_SCRIPT

       deploy/README.md, "Alerting on a failing source", has the details.
MISSING
    exit 1
fi

SRE_TAB_ALERT_UNIT=$unit
SRE_TAB_ALERT_RESULT=${result:-unknown}
SRE_TAB_ALERT_STATUS=${status:-unknown}
SRE_TAB_ALERT_HOST=$host
export SRE_TAB_ALERT_UNIT SRE_TAB_ALERT_RESULT SRE_TAB_ALERT_STATUS SRE_TAB_ALERT_HOST

printf '%s\n' "$report" | "$ALERT_SCRIPT" "$unit"
