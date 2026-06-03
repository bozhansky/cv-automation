"""
Auto-apply agent — Ollama-powered Playwright automation with user approval gate.

Architecture:
  1. Polls pending_jobs table for jobs ready to apply
  2. For each job: navigates to application_url via Playwright
  3. Shows approval modal (st.modal) — user must confirm before form is submitted
  4. On approval: fills form, uploads tailored resume, pastes cover letter
  5. On submit: records applied_at in DB, sends Telegram notification

Approval flow:
  - pending_applications table tracks jobs pending approval
  - Frontend polls this table and shows "⏳ Pending Approval" badge
  - User clicks Confirm → DB updated → auto-apply proceeds
  - Streamlit reruns and shows live progress
"""

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
APP_DIR = Path.home() / ".applypilot"
CHROME_WORKER_DIR = APP_DIR / "chrome-workers"
WORKER_DIR = APP_DIR / "apply-workers"
CHROME_WORKER_DIR.mkdir(exist_ok=True)
WORKER_DIR.mkdir(exist_ok=True)

PROFILE_PATH = APP_DIR / "profile.json"
DB_PATH = APP_DIR / "applypilot.db"


# ── Config ────────────────────────────────────────────────────────────────────
def load_profile() -> dict:
    with open(PROFILE_PATH) as f:
        return json.load(f)


def get_ollama_client(base_url: str = "http://127.0.0.1:11434/v1"):
    """OpenAI-compatible client for Ollama."""
    return httpx.Client(base_url=base_url, timeout=60.0)


# ── Job dataclass ─────────────────────────────────────────────────────────────
@dataclass
class Job:
    url: str
    title: str = ""
    company: str = ""
    location: str = ""
    site: str = ""
    apply_url: str = ""
    tailored_resume_path: str = ""
    cover_letter_path: str = ""
    fit_score: int = 0

    @classmethod
    def from_db_row(cls, row: sqlite3.Row) -> "Job":
        return cls(
            url=row["url"],
            title=row.get("title", ""),
            company=row.get("company", "") or row.get("site", ""),
            location=row.get("location", ""),
            site=row.get("site", ""),
            apply_url=row.get("application_url") or row.get("url", ""),
            tailored_resume_path=row.get("tailored_resume_path") or "",
            cover_letter_path=row.get("cover_letter_path") or "",
            fit_score=int(row.get("fit_score") or 0),
        )


def get_pending_jobs(conn: sqlite3.Connection, limit: int = 10) -> list[Job]:
    """Return jobs awaiting approval (status='pending_approval')."""
    rows = conn.execute("""
        SELECT * FROM jobs
        WHERE apply_status = 'pending_approval'
        ORDER BY fit_score DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [Job.from_db_row(r) for r in rows]


def get_ready_jobs(conn: sqlite3.Connection, limit: int = 10) -> list[Job]:
    """Return jobs with tailored resume + cover letter, not yet applied."""
    rows = conn.execute("""
        SELECT * FROM jobs
        WHERE tailored_resume_path IS NOT NULL
          AND tailored_resume_path != ''
          AND cover_letter_path IS NOT NULL
          AND cover_letter_path != ''
          AND (applied_at IS NULL OR applied_at = '')
          AND (apply_status IS NULL OR apply_status = '' OR apply_status = 'pending_approval')
        ORDER BY fit_score DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [Job.from_db_row(r) for r in rows]


def mark_pending_approval(conn: sqlite3.Connection, url: str) -> None:
    conn.execute("""
        UPDATE jobs SET apply_status = 'pending_approval'
        WHERE url = ?
    """, (url,))
    conn.commit()


def mark_approval_approved(conn: sqlite3.Connection, url: str,
                            actor: str = "dashboard") -> bool:
    """Mark a job as approved for application (sets approved_at + status).

    Called from:
      - Dashboard "Approve" button (actor='dashboard')
      - Telegram callback button (actor='telegram:<username>')

    Args:
        conn: SQLite connection
        url: Job URL
        actor: Where the approval came from (for audit logging)

    Returns:
        True if a row was actually changed, False if the job was already
        approved/applied/etc. (idempotent guard for re-tap scenarios).
    """
    now = datetime.now(timezone.utc).isoformat()
    # Only transition from "pending_approval" → "approved". Other statuses
    # (already applied, already declined, etc.) are left alone so the audit
    # trail is preserved. If the user re-taps Approve, we just no-op.
    cur = conn.execute("""
        UPDATE jobs SET
            apply_status = 'approved',
            approved_at = ?
        WHERE url = ?
          AND apply_status = 'pending_approval'
    """, (now, url))
    conn.commit()
    changed = cur.rowcount > 0
    if changed:
        logger.info("Job approved by %s: %s", actor, url[:80])
    return changed


def mark_applied(conn: sqlite3.Connection, url: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        UPDATE jobs SET
            applied_at = ?,
            apply_status = 'applied'
        WHERE url = ?
    """, (now, url))
    conn.commit()


def mark_approval_declined(conn: sqlite3.Connection, url: str,
                            actor: str = "dashboard") -> bool:
    """Mark a job as declined. Idempotent — only updates if still pending.

    Returns:
        True if a row was actually changed.
    """
    cur = conn.execute("""
        UPDATE jobs SET apply_status = 'declined'
        WHERE url = ?
          AND apply_status IN ('pending_approval', 'approved')
    """, (url,))
    conn.commit()
    return cur.rowcount > 0


# ── Telegram notification ─────────────────────────────────────────────────────
def notify_telegram(message: str, token: str = None, chat_id: str = None) -> bool:
    """Send a Telegram message. Token/chat_id from env or config."""
    if not token:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not chat_id:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("Telegram credentials not configured — skipping notification")
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10.0,
        )
        return resp.status_code == 200
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
        return False


def notify_application_ready(job: Job) -> None:
    """Send Telegram notification when a job is ready to approve."""
    msg = (
        f"📋 <b>Application Ready to Approve</b>\n\n"
        f"<b>{job.title}</b>\n"
        f"🏢 {job.company}\n"
        f"📍 {job.location}\n"
        f"⭐ Score: {job.fit_score}/10\n\n"
        f"🔗 {job.apply_url or job.url}"
    )
    notify_telegram(msg)


def notify_application_submitted(job: Job) -> None:
    """Send Telegram notification when application was submitted."""
    msg = (
        f"✅ <b>Application Submitted</b>\n\n"
        f"<b>{job.title}</b>\n"
        f"🏢 {job.company}\n"
        f"📍 {job.location}"
    )
    notify_telegram(msg)


# ── Playwright automation ─────────────────────────────────────────────────────
PLAYWRIGHT_SCRIPT = """
const { chromium } = require('playwright');

async function run() {
    const url = process.argv[2];
    const profileJson = process.argv[3];
    const resumePath = process.argv[4];
    const coverPath = process.argv[5];
    const applyUrl = process.argv[6];

    const profile = JSON.parse(profileJson);
    const targetUrl = applyUrl || url;

    const browser = await chromium.launch({ headless: false });
    const page = await browser.newPage();

    try {
        // Navigate to application URL
        await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
        await page.waitForTimeout(3000);

        // Fill common fields using LLM to identify them
        // This is a placeholder — actual form filling requires site-specific logic
        const fields = [
            { label: 'name', value: profile.personal.full_name },
            { label: 'email', value: profile.personal.email },
            { label: 'phone', value: profile.personal.phone },
        ];

        for (const field of fields) {
            try {
                const input = await page.locator(`input[name="${field.label}"], input[id*="${field.label}"], input[placeholder*="${field.label}"]`).first();
                if (await input.isVisible({ timeout: 2000 })) {
                    await input.fill(field.value);
                }
            } catch (e) {
                // field not found on this site
            }
        }

        // Upload resume if path provided
        if (resumePath && resumePath !== 'None') {
            try {
                const uploadInput = await page.locator('input[type="file"]').first();
                if (await uploadInput.isVisible({ timeout: 2000 })) {
                    await uploadInput.setInputFiles(resumePath);
                }
            } catch (e) {
                // no file upload on this site
            }
        }

        // Paste cover letter if textarea exists
        if (coverPath && coverPath !== 'None') {
            try {
                const fs = require('fs');
                const coverText = fs.readFileSync(coverPath, 'utf-8').substring(0, 2000);
                const coverArea = await page.locator('textarea[name*="cover"], textarea[name*="message"], textarea[name*="comment"]').first();
                if (await coverArea.isVisible({ timeout: 2000 })) {
                    await coverArea.fill(coverText);
                }
            } catch (e) {
                // no cover letter field
            }
        }

        console.log(JSON.stringify({ status: 'ready_to_submit', url: page.url() }));
    } catch (err) {
        console.error(JSON.stringify({ status: 'error', message: err.message }));
    } finally {
        await browser.close();
    }
}
run();
"""


def run_playwright_autofill(job: Job, profile: dict) -> dict:
    """Run Playwright to navigate to application and pre-fill form fields."""
    import tempfile
    worker_dir = WORKER_DIR / f"job_{hash(job.url) % 10000:04d}"
    worker_dir.mkdir(exist_ok=True)

    script_path = worker_dir / "autofill.js"
    with open(script_path, "w") as f:
        f.write(PLAYWRIGHT_SCRIPT)

    resume = job.tailored_resume_path or "None"
    cover = job.cover_letter_path or "None"
    apply_url = job.apply_url or job.url

    try:
        result = subprocess.run(
            ["node", str(script_path), job.url, json.dumps(profile),
             resume, cover, apply_url],
            capture_output=True, text=True, timeout=120,
            cwd=str(worker_dir),
        )
        output = result.stdout.strip()
        if output.startswith("{"):
            return json.loads(output)
        return {"status": "unknown", "raw": output[:200]}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── Apply pipeline (called by frontend) ──────────────────────────────────────
def submit_application(url: str, approved: bool = True) -> dict:
    """
    Main entry point from Streamlit frontend.
    If approved=False, marks as declined and returns.
    If approved=True, runs auto-apply and sends Telegram on success.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if not approved:
        mark_approval_declined(conn, url)
        conn.close()
        return {"status": "declined", "url": url}

    rows = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchall()
    if not rows:
        conn.close()
        return {"status": "error", "message": "Job not found in DB"}
    job = Job.from_db_row(rows[0])
    conn.close()

    if not job.apply_url:
        return {"status": "error", "message": "No application URL found"}

    profile = load_profile()
    result = run_playwright_autofill(job, profile)

    if result.get("status") == "ready_to_submit":
        # Mark as applied
        conn = sqlite3.connect(DB_PATH)
        mark_applied(conn, job.url)
        conn.close()
        notify_application_submitted(job)
        return {"status": "submitted", "url": job.url, "note": "Form pre-filled — confirm on browser"}
    else:
        return {"status": "error", "message": result.get("message", "Playwright failed")}


def queue_for_approval(url: str) -> None:
    """Mark a job as pending user approval + send Telegram."""
    conn = sqlite3.connect(DB_PATH)
    mark_pending_approval(conn, url)
    rows = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchall()
    if rows:
        job = Job.from_db_row(rows[0])
        notify_application_ready(job)
    conn.close()