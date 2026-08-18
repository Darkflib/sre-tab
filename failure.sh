#!/bin/sh
set -eu
backup_timer_was_active=true
database_restored=false
release_backup_timer() {
    [ "$backup_timer_was_active" = true ] || return 0
    if [ "$database_restored" = true ]; then echo "TIMER RESTARTED"; return 0; fi
    echo "TIMER LEFT STOPPED (restore incomplete)"
}
trap release_backup_timer EXIT
echo "restoring..."; false; echo "unreachable"
