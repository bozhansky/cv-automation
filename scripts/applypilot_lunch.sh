#!/bin/bash
# ApplyPilot midday shortlist — discover + enrich + score, no tailoring.
# Runs at 12:00 daily. Catches morning's new jobs so you can review
# high-fit roles in the afternoon before the 20:00 full pipeline.
#
# Why this exists:
#   - 20:00 full pipeline runs discover + enrich + score + tailor + cover (~4-6h)
#   - This midday run gives you a fresh shortlist ~8h earlier
#   - Tailor+cover are still done by the 20:00 cron
#
# Logs: ~/.applypilot/cron-lunch.log

set -uo pipefail

export HOME=/home/bostjan
export LLM_URL="http://127.0.0.1:11434/v1"
export LLM_MODEL="gemma4:31b-cloud"
export LLM_API_KEY="not-needed"

LOCK=/tmp/applypilot_lunch.lock
LOG=/home/bostjan/.applypilot/cron-lunch.log
TS=$(date '+%Y-%m-%d %H:%M:%S')

exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[$TS] Another lunch pipeline is already running — skipping." >> "$LOG"
    exit 0
fi

# Health check: ping Ollama first.
OLLAMA_PING=$(curl -s --max-time 10 -o /dev/null -w "%{http_code}" \
    -X POST "$LLM_URL/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{"model":"'$LLM_MODEL'","messages":[{"role":"user","content":"ping"}],"max_tokens":4}' 2>/dev/null || echo "000")

if [ "$OLLAMA_PING" != "200" ]; then
    echo "[$TS] Ollama health check FAILED (HTTP $OLLAMA_PING) — skipping run." >> "$LOG"
    exit 1
fi

echo "[$TS] === ApplyPilot midday STARTING (discover + enrich + score) ===" >> "$LOG"

cd "/media/bostjan/Documents/Osebno/ZAPOSLITEV/AI JOB 2026"

if python3 -m applypilot run discover enrich score >> "$LOG" 2>&1; then
    TS_END=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$TS_END] === ApplyPilot midday COMPLETED OK ===" >> "$LOG"
    exit 0
else
    TS_END=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$TS_END] === ApplyPilot midday FAILED (exit $?) ===" >> "$LOG"
    exit 1
fi
