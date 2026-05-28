#!/usr/bin/env python3
"""
Telegram notification script — run after pipeline stages.
Checks DB for new "ready to apply" jobs and sends a Telegram alert.
Can be wired into cron or called by the pipeline after `tailor` + `cover` stages.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path.home() / ".applypilot"
DB_PATH = APP_DIR / "applypilot.db"
PROFILE_PATH = APP_DIR / "profile.json"


def get_ready_jobs(conn: sqlite3.Connection, min_score: int = 7):
    rows = conn.execute("""
        SELECT * FROM jobs
        WHERE fit_score >= ?
          AND (tailored_resume_path IS NULL OR tailored_resume_path = '')
          AND (applied_at IS NULL OR applied_at = '')
          AND (apply_status IS NULL OR apply_status = '')
        ORDER BY fit_score DESC
    """, (min_score,)).fetchall()
    return rows


def notify(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping")
        return False
    import httpx
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


def main():
    if len(sys.argv) > 1:
        min_score = int(sys.argv[1])
    else:
        min_score = 7

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    ready = conn.execute("""
        SELECT COUNT(*) FROM jobs
        WHERE fit_score >= ? AND applied_at IS NULL
          AND tailored_resume_path IS NOT NULL AND tailored_resume_path != ''
          AND cover_letter_path IS NOT NULL AND cover_letter_path != ''
    """, (min_score,)).fetchone()[0]
    pending = conn.execute("""
        SELECT COUNT(*) FROM jobs
        WHERE apply_status = 'pending_approval'
    """).fetchone()[0]

    msg = (
        f"📊 <b>ApplyPilot Pipeline Update</b>\n\n"
        f"Jobs discovered: {total}\n"
        f"Ready to apply (score ≥{min_score}): {ready}\n"
        f"Pending your approval: {pending}\n\n"
        f"Open the app → http://localhost:8501"
    )
    notify(msg)
    print(f"Notified. Total: {total}, Ready: {ready}, Pending: {pending}")


if __name__ == "__main__":
    main()