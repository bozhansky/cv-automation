#!/bin/bash
# ApplyPilot weekly cleanup — purge jobs older than 7 days.
# Runs Sundays at 03:00 (low-traffic time).
#
# Removes:
#   - Jobs with discovered_at < (now - 7 days)
#   - Their tailored resume files
#   - Their cover letter files
# Preserves:
#   - Jobs you've already applied to (use --include-applied to override)
#
# Logs: ~/.applypilot/cron-purge.log
# Notification: delivered to current chat on completion.

set -uo pipefail

export HOME=/home/bostjan
export LLM_URL="http://127.0.0.1:11434/v1"
export LLM_MODEL="gemma4:31b-cloud"
export LLM_API_KEY="not-needed"

LOG=/home/bostjan/.applypilot/cron-purge.log
TS=$(date '+%Y-%m-%d %H:%M:%S')

echo "[$TS] === ApplyPilot weekly purge STARTING (older-than-days=7) ===" >> "$LOG"

cd "/media/bostjan/Documents/Osebno/ZAPOSLITEV/AI JOB 2026"

if python3 -m applypilot purge --older-than-days 7 >> "$LOG" 2>&1; then
    TS_END=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$TS_END] === ApplyPilot weekly purge COMPLETED OK ===" >> "$LOG"
    exit 0
else
    TS_END=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$TS_END] === ApplyPilot weekly purge FAILED (exit $?) ===" >> "$LOG"
    exit 1
fi
