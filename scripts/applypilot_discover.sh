#!/bin/bash
# ApplyPilot periodic discovery — runs every 4 hours.
# Uses a lockfile to prevent stacking (watchdog pattern).
#
# Cron entry: 0 */4 * * *  (every 4 hours at :00)
# This means: 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 daily.
#
# Why every 4h:
#   - The 20:00 daily cron also runs discover, so this fills the gap.
#   - Most job-board jobs stay fresh for 6-12h, so 4h catches them.
#   - With a lockfile, a hung process from the previous tick won't stack up.
#
# Logs: ~/.applypilot/cron-discover.log

set -uo pipefail

export HOME=/home/bostjan
export LLM_URL="http://127.0.0.1:11434/v1"
export LLM_MODEL="gemma4:31b-cloud"
export LLM_API_KEY="not-needed"

LOCK=/tmp/applypilot_discover.lock
LOG=/home/bostjan/.applypilot/cron-discover.log
TS=$(date '+%Y-%m-%d %H:%M:%S')

# Watchdog: exit immediately if another discover is running.
# The lock file is held by the running process via FD; we use flock on a
# descriptor so the lock auto-releases on process exit (no stale locks).
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[$TS] Another discover is already running — skipping." >> "$LOG"
    exit 0
fi

echo "[$TS] === ApplyPilot periodic discover STARTING ===" >> "$LOG"

cd "/media/bostjan/Documents/Osebno/ZAPOSLITEV/AI JOB 2026"

if python3 -m applypilot run discover >> "$LOG" 2>&1; then
    TS_END=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$TS_END] === ApplyPilot periodic discover COMPLETED OK ===" >> "$LOG"
    exit 0
else
    TS_END=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$TS_END] === ApplyPilot periodic discover FAILED (exit $?) ===" >> "$LOG"
    exit 1
fi
