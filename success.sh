#!/bin/sh
set -eu
backup_timer_was_active=true
database_restored=false
release_backup_timer() {
    [ "$backup_timer_was_active" = true ] || return 0
    if [ "$database_restored" = true ]; then echo "TIMER RESTARTED"; return 0; fi
    echo "TIMER LEFT STOPPED (restore incomplete)"
}
on_signal() { trap - EXIT "$1"; release_backup_timer; kill -s "$1" $$; }
trap release_backup_timer EXIT
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
echo "restoring..."; database_restored=true; echo "verified"
