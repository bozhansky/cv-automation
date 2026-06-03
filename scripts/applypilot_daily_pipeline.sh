#!/bin/bash
# ApplyPilot daily full-pipeline cron job.
# Runs at 19:55 daily. Processes only jobs discovered in the last 24h
# (20:00 previous evening -> 19:55 today).
#
# Stages: discover -> enrich -> score -> tailor -> cover
# Min score for tailor/cover: 7
#
# Logs: ~/.applypilot/cron-pipeline.log
# Notification: delivered to current chat on completion.

set -uo pipefail

export HOME=/home/bostjan
export LLM_URL="http://127.0.0.1:11434/v1"
export LLM_MODEL="gemma4:31b-cloud"
export LLM_API_KEY="not-needed"

LOG=/home/bostjan/.applypilot/cron-pipeline.log
TS=$(date '+%Y-%m-%d %H:%M:%S')

# ── Health check: ping Ollama before launching the heavy pipeline ────────
OLLAMA_PING=$(curl -s --max-time 10 -o /dev/null -w "%{http_code}" \
    -X POST "$LLM_URL/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{"model":"'$LLM_MODEL'","messages":[{"role":"user","content":"ping"}],"max_tokens":4}' 2>/dev/null || echo "000")

if [ "$OLLAMA_PING" != "200" ]; then
    echo "[$TS] Ollama health check FAILED (HTTP $OLLAMA_PING) — skipping run." >> "$LOG"
    exit 1
fi
echo "[$TS] Ollama health check OK." >> "$LOG"

# Compute 24h window start: 20:00 yesterday (local time, then convert to UTC)
# Cron is local time; applypilot expects ISO-8601 in UTC for `discovered_at` comparison.
SINCE=$(date -d 'yesterday 20:00:00' '+%Y-%m-%dT%H:%M:%S')
SINCE_ISO="$SINCE"

echo "[$TS] === ApplyPilot daily pipeline STARTING ===" >> "$LOG"
echo "[$TS] Window: discovered_at >= $SINCE_ISO" >> "$LOG"
echo "[$TS] Command: cd '/media/bostjan/Documents/Osebno/ZAPOSLITEV/AI JOB 2026' && python3 -m applypilot run all --min-score 7 --since '$SINCE_ISO'" >> "$LOG"

cd "/media/bostjan/Documents/Osebno/ZAPOSLITEV/AI JOB 2026"

if python3 -m applypilot run all --min-score 7 --since "$SINCE_ISO" >> "$LOG" 2>&1; then
    TS_END=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$TS_END] === ApplyPilot daily pipeline COMPLETED OK ===" >> "$LOG"
    exit 0
else
    TS_END=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$TS_END] === ApplyPilot daily pipeline FAILED (exit $?) ===" >> "$LOG"
    exit 1
fi
