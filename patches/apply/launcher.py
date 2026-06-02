"""Apply orchestration: acquire jobs, run apply agents, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + the apply agent for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import json
import logging
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.database import get_connection
from applypilot.apply import chrome, dashboard, prompt as prompt_mod
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals,
)

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track active legacy CLI processes for skip (Ctrl+C) handling
_legacy_procs: dict[int, subprocess.Popen] = {}
_legacy_lock = threading.Lock()

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            },
            "gmail": {
                "command": "npx",
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            },
        }
    }


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            like = f"%{target_url.split('?')[0].rstrip('/')}%"
            row = conn.execute("""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                  AND tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status != 'in_progress')
                LIMIT 1
            """, (target_url, target_url, like, like)).fetchone()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            # Build parameterized filters to avoid SQL injection
            params: list = [min_score]
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join(f"AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            row = conn.execute(f"""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status = 'failed')
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND fit_score >= ?
                  {site_clause}
                  {url_clauses}
                ORDER BY fit_score DESC, url
                LIMIT 1
            """, [config.DEFAULTS["max_apply_attempts"]] + params).fetchone()

        if not row:
            conn.rollback()
            return None

        # Skip manual ATS sites (unsolvable CAPTCHAs)
        from applypilot.config import is_manual_ats
        apply_url = row["application_url"] or row["url"]
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
            WHERE url = ?
        """, (f"worker-{worker_id}", now, row["url"]))
        conn.commit()

        return dict(row)
    except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None, cost_usd: float | None = None) -> None:
    """Update a job's apply status in the database.

    On `status == 'applied'`, fires a Telegram notification (if configured)
    in a background thread. Notification failures never block the apply.
    """
    # Fetch job details BEFORE updating so we have title/company for the notifier.
    job_for_notify: dict | None = None
    if status == "applied":
        try:
            from applypilot.apply.notifier import notify_applied
            conn0 = get_connection()
            row = conn0.execute(
                "SELECT url, title, company, site FROM jobs WHERE url = ? LIMIT 1",
                (url,),
            ).fetchone()
            if row:
                keys = ("url", "title", "company", "site")
                job_for_notify = dict(zip(keys, row))
        except Exception as e:
            logger.debug("mark_result: failed to fetch job for notifier: %s", e)

    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           apply_cost_usd = COALESCE(?, apply_cost_usd)
            WHERE url = ?
        """, (now, duration_ms, task_id, cost_usd, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           apply_cost_usd = COALESCE(?, apply_cost_usd)
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, cost_usd, url))
    conn.commit()

    # Fire-and-forget Telegram notification on successful apply.
    if status == "applied" and job_for_notify is not None:
        try:
            from applypilot.apply.notifier import notify_applied
            notify_applied(job_for_notify, duration_ms=duration_ms, cost_usd=cost_usd)
        except Exception as e:
            # Never let a notifier error break the apply pipeline.
            logger.debug("mark_result: notify_applied raised: %s", e)

    # Optional failure notification (off by default; opt-in via env var).
    elif status == "failed" and job_for_notify is not None:
        try:
            from applypilot.apply.notifier import notify_failed
            notify_failed(job_for_notify, error=error)
        except Exception as e:
            logger.debug("mark_result: notify_failed raised: %s", e)

    # Update form schema cache usage counters (4.8) — track per-site success rate
    # so we know which cached schemas actually work.
    if job_for_notify is not None and status in ("applied", "failed", "expired",
                                                   "captcha", "login_issue"):
        try:
            from applypilot.apply.form_schema_cache import update_usage
            update_usage(job_for_notify.get("site"), success=(status == "applied"))
        except Exception as e:
            logger.debug("mark_result: form_schema_cache update_usage raised: %s", e)


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 7,
               model: str = "ollama-default", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the external CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    safe_title = re.sub(r'[^\w\-.]', '_', job["title"])[:30]
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{safe_title}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def preflight_check(job: dict) -> tuple[bool, str]:
    """Verify a job has everything it needs before launching the apply agent.

    Returns (ok, reason). If `ok` is False, the apply should be skipped and
    the reason will be recorded as apply_error.

    Checks:
      1. tailored_resume_path is set
      2. tailored_resume_path PDF file actually exists on disk, is non-empty,
         and has a valid PDF header (%PDF-)
      3. cover_letter_path is set
      4. cover_letter_path PDF file actually exists on disk, is non-empty,
         and has a valid PDF header (%PDF-)
      5. application_url or url is set (and parseable)
      6. fit_score is at least 7 (don't apply to low-fit jobs)
      7. tailored_resume_path doesn't point outside ~/.applypilot/
         (defense-in-depth against path traversal)

    Idempotent. Cheap. Run before each apply.
    """
    # Allow disabling the file check via env (useful for testing)
    if os.environ.get("APPLY_SKIP_FILE_CHECK", "").strip() in ("1", "true", "yes"):
        return _preflight_minimal(job)

    if not job.get("tailored_resume_path"):
        return False, "preflight: no tailored_resume_path"
    resume_path = Path(job["tailored_resume_path"])
    file_ok, reason = _validate_pdf_file(resume_path, "tailored resume")
    if not file_ok:
        return False, reason
    if not _is_path_safe(resume_path):
        return False, f"preflight: tailored_resume_path outside data dir: {resume_path}"

    if not job.get("cover_letter_path"):
        return False, "preflight: no cover_letter_path"
    cover_path = Path(job["cover_letter_path"])
    file_ok, reason = _validate_pdf_file(cover_path, "cover letter")
    if not file_ok:
        return False, reason

    apply_url = job.get("application_url") or job.get("url")
    if not apply_url or not apply_url.startswith(("http://", "https://")):
        return False, f"preflight: bad application_url: {apply_url!r}"
    score = job.get("fit_score")
    if score is not None and score < 7:
        return False, f"preflight: fit_score too low ({score} < 7)"

    # Dynamic blacklist check (4.10) — only if the env var is enabled
    if os.environ.get("APPLY_ENABLE_BLACKLIST", "").strip() in ("1", "true", "yes"):
        try:
            from applypilot.database import is_site_blacklisted
            site = job.get("site")
            blacklisted, reason = is_site_blacklisted(site)
            if blacklisted:
                return False, f"preflight: {reason}"
        except Exception as e:
            logger.debug("preflight: blacklist check failed: %s", e)

    return True, "ok"


def _preflight_minimal(job: dict) -> tuple[bool, str]:
    """Minimal preflight (used when APPLY_SKIP_FILE_CHECK=1). Only checks URL + score."""
    apply_url = job.get("application_url") or job.get("url")
    if not apply_url or not apply_url.startswith(("http://", "https://")):
        return False, f"preflight: bad application_url: {apply_url!r}"
    score = job.get("fit_score")
    if score is not None and score < 7:
        return False, f"preflight: fit_score too low ({score} < 7)"
    return True, "ok"


def _validate_pdf_file(path: Path, kind: str) -> tuple[bool, str]:
    """Verify a PDF file exists, is non-empty, and starts with the PDF magic header.

    Returns (ok, reason). On failure, reason names the file + problem so the
    apply error makes it easy to debug.
    """
    if not path.exists():
        return False, f"preflight: {kind} file missing: {path}"
    try:
        size = path.stat().st_size
    except OSError as e:
        return False, f"preflight: {kind} file stat failed: {e}"
    if size == 0:
        return False, f"preflight: {kind} file is 0 bytes: {path}"
    # Quick header check — first 5 bytes should be %PDF-
    try:
        with open(path, "rb") as f:
            header = f.read(5)
        if header != b"%PDF-":
            return False, f"preflight: {kind} file is not a PDF (header={header!r}): {path}"
    except OSError as e:
        return False, f"preflight: {kind} file open failed: {e}"
    return True, "ok"


def _is_path_safe(path: Path) -> bool:
    """Verify path is inside the applypilot data dir (defense-in-depth).

    Accepts paths inside any of:
      - config.APP_DIR
      - config.TAILORED_DIR
      - config.COVER_LETTER_DIR
      - any /tmp directory (for test fixtures)

    Handles symlinks by resolving both sides before comparison.
    """
    try:
        path = path.resolve()
    except (OSError, RuntimeError):
        return False
    # Always-allowed roots — resolve to handle symlinks like ~/.applypilot
    allowed_roots = []
    for attr in ("APP_DIR", "TAILORED_DIR", "COVER_LETTER_DIR", "DATA_DIR"):
        d = getattr(config, attr, None)
        if d is not None:
            try:
                allowed_roots.append(Path(d).resolve())
            except (OSError, RuntimeError):
                pass
    if not allowed_roots:
        try:
            allowed_roots.append((Path.home() / ".applypilot").resolve())
        except (OSError, RuntimeError):
            pass
    for root in allowed_roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    # Test fixtures often live in /tmp
    try:
        path.relative_to(Path("/tmp").resolve())
        return True
    except ValueError:
        pass
    return False



def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "ollama-default", dry_run: bool = False) -> tuple[str, int, float]:
    """Spawn an apply agent for one job application.

    Routes to the Ollama-based Python agent loop when APPLY_AGENT=ollama
    (default in this fork), or falls back to spawning the external legacy CLI
    subprocess when APPLY_AGENT=legacy-cli (alias: legacy-cli).

    Returns:
        Tuple of (status_string, duration_ms, cost_usd). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    # ── Preflight: skip the job if anything is missing ───────────────────
    ok, reason = preflight_check(job)
    if not ok:
        log.warning("Preflight failed for %s: %s", job.get("url"), reason)
        mark_result(job["url"], "failed:preflight", error=reason, permanent=True)
        return f"skipped:{reason}", 0, 0.0

    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    # Build the prompt
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
    )

    # Update dashboard state before agent starts
    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    # === Agent routing ===
    apply_agent = os.environ.get("APPLY_AGENT", "ollama").lower()

    if apply_agent in ("legacy-cli", "claude"):  # legacy-cli = canonical, claude = old alias
        return _run_job_legacy_cli(job, port, worker_id, model, agent_prompt)

    if apply_agent == "mcp":
        return _run_job_mcp(job, port, worker_id, model, dry_run, agent_prompt)

    # Default: Ollama-based Python agent loop
    from applypilot.apply.ollama_agent import run_ollama_agent, DEFAULT_MODEL
    cdp_url = f"http://127.0.0.1:{port}"
    worker_dir = reset_worker_dir(worker_id)
    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"Agent: Ollama ({model or DEFAULT_MODEL})\n"
        f"{'=' * 60}\n"
    )
    text_parts: list[str] = []
    actions_count = [0]

    def _emit_event(line: str):
        text_parts.append(line)
        try:
            with open(worker_log, "a", encoding="utf-8") as lf:
                lf.write(line + "\n")
        except Exception:
            pass
        try:
            obj = json.loads(line)
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    logger.info("LLM: %s", block["text"][:200])
                elif block.get("type") == "tool_use":
                    actions_count[0] += 1
                    inp = block.get("input", {})
                    name = block.get("name", "")
                    if "url" in inp:
                        desc = f"{name} {inp['url'][:60]}"
                    elif "ref" in inp:
                        desc = f"{name} {inp.get('element', inp['ref'])}"[:50]
                    elif "fields" in inp:
                        desc = f"{name} ({len(inp['fields'])} fields)"
                    else:
                        desc = name
                    update_state(worker_id, actions=actions_count[0], last_action=desc[:35])
                    logger.info("Tool: %s", desc)
        except json.JSONDecodeError:
            pass

    try:
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)
    except Exception:
        pass

    finish_status, duration_ms, stats = run_ollama_agent(
        prompt=agent_prompt,
        cdp_url=cdp_url,
        worker_dir=worker_dir,
        worker_id=worker_id,
        model=model or DEFAULT_MODEL,
        dry_run=dry_run,
        emit_event=_emit_event,
    )

    # Save full output log
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    job_log = config.LOG_DIR / f"ollama_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
    try:
        job_log.write_text("\n".join(text_parts), encoding="utf-8")
    except Exception as e:
        logger.warning("Could not write job log: %s", e)

    # Normalize status string (matches what the external CLI returned)
    status_map = {
        "APPLIED": "applied",
        "EXPIRED": "expired",
        "CAPTCHA": "captcha",
        "LOGIN_ISSUE": "login_issue",
        "FAILED": None,  # handled below
    }
    elapsed = duration_ms // 1000
    if finish_status in status_map and status_map[finish_status] is not None:
        out_status = status_map[finish_status]
        add_event(f"[W{worker_id}] {finish_status} ({elapsed}s): {job['title'][:30]}")
        update_state(worker_id, status=out_status,
                     last_action=f"{finish_status} ({elapsed}s)")
    else:
        # FAILED with a reason
        clean_reason = re.sub(r'[*`"]+$', '', finish_reason).strip()[:50] or "unknown"
        PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue"}
        if clean_reason in PROMOTE_TO_STATUS:
            out_status = clean_reason
            add_event(f"[W{worker_id}] {clean_reason.upper()} ({elapsed}s): {job['title'][:30]}")
            update_state(worker_id, status=out_status,
                         last_action=f"{clean_reason.upper()} ({elapsed}s)")
        else:
            out_status = f"failed:{clean_reason}"
            add_event(f"[W{worker_id}] FAILED ({elapsed}s): {clean_reason[:30]}")
            update_state(worker_id, status="failed",
                         last_action=f"FAILED: {clean_reason[:25]}")

    # Update total cost (estimated). Ollama is local, but we still record the
    # token-based estimate for stats tracking and per-job cost caps.
    cost = 0.0
    if stats:
        cost = (
            stats.get("input_tokens", 0) * 3e-6
            + stats.get("output_tokens", 0) * 15e-6
        )
    ws = get_state(worker_id)
    prev_cost = ws.total_cost if ws else 0.0
    update_state(worker_id, total_cost=prev_cost + cost)

    return out_status, duration_ms, cost


def _run_job_legacy_cli(job: dict, port: int, worker_id: int,
                        model: str, agent_prompt: str) -> tuple[str, int, float]:
    """Original external-CLI-based apply path (kept for fallback)."""
    # Write per-worker MCP config
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")
    # Cost is metered by the third-party LLM provider; we report 0.0 for this path.
    # (Future: parse the legacy CLI usage output for token counts.)
    _PLACEHOLDER = (port, model)  # silence unused-arg warnings
    del _PLACEHOLDER

    # Build legacy CLI command
    cmd = [
        "legacy-cli",
        "--model", model,
        "-p",
        "--mcp-config", str(mcp_config_path),
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--disallowedTools", (
            "mcp__gmail__draft_email,mcp__gmail__modify_email,"
            "mcp__gmail__delete_email,mcp__gmail__download_attachment,"
            "mcp__gmail__batch_modify_emails,mcp__gmail__batch_delete_emails,"
            "mcp__gmail__create_label,mcp__gmail__update_label,"
            "mcp__gmail__delete_label,mcp__gmail__get_or_create_label,"
            "mcp__gmail__list_email_labels,mcp__gmail__create_filter,"
            "mcp__gmail__list_filters,mcp__gmail__get_filter,"
            "mcp__gmail__delete_filter"
        ),
        "--output-format", "stream-json",
        "--verbose", "-",
    ]

    env = os.environ.copy()
    # Defensive: strip any legacy-CLI env-var keys if we're nested inside
    # a parent process that set them. Prevents nested-process confusion.
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    worker_dir = reset_worker_dir(worker_id)

    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
        )
        with _legacy_lock:
            _legacy_procs[worker_id] = proc

        proc.stdin.write(agent_prompt)
        proc.stdin.close()

        text_parts: list[str] = []
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type")
                    if msg_type == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            bt = block.get("type")
                            if bt == "text":
                                text_parts.append(block["text"])
                                lf.write(block["text"] + "\n")
                            elif bt == "tool_use":
                                name = (
                                    block.get("name", "")
                                    .replace("mcp__playwright__", "")
                                    .replace("mcp__gmail__", "gmail:")
                                )
                                inp = block.get("input", {})
                                if "url" in inp:
                                    desc = f"{name} {inp['url'][:60]}"
                                elif "ref" in inp:
                                    desc = f"{name} {inp.get('element', inp.get('text', ''))}"[:50]
                                elif "fields" in inp:
                                    desc = f"{name} ({len(inp['fields'])} fields)"
                                elif "paths" in inp:
                                    desc = f"{name} upload"
                                else:
                                    desc = name

                                lf.write(f"  >> {desc}\n")
                                ws = get_state(worker_id)
                                cur_actions = ws.actions if ws else 0
                                update_state(worker_id,
                                             actions=cur_actions + 1,
                                             last_action=desc[:35])
                    elif msg_type == "result":
                        stats = {
                            "input_tokens": msg.get("usage", {}).get("input_tokens", 0),
                            "output_tokens": msg.get("usage", {}).get("output_tokens", 0),
                            "cache_read": msg.get("usage", {}).get("cache_read_input_tokens", 0),
                            "cache_create": msg.get("usage", {}).get("cache_creation_input_tokens", 0),
                            "cost_usd": msg.get("total_cost_usd", 0),
                            "turns": msg.get("num_turns", 0),
                        }
                        text_parts.append(msg.get("result", ""))
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(line + "\n")

        proc.wait(timeout=300)
        returncode = proc.returncode
        proc = None

        if returncode and returncode < 0:
            return "skipped", int((time.time() - start) * 1000)

        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"legacy_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        def _clean_reason(s: str) -> str:
            return re.sub(r'[*`"]+$', '', s).strip()

        for result_status in ["APPLIED", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE"]:
            if f"RESULT:{result_status}" in output:
                add_event(f"[W{worker_id}] {result_status} ({elapsed}s): {job['title'][:30]}")
                update_state(worker_id, status=result_status.lower(),
                             last_action=f"{result_status} ({elapsed}s)")
                return result_status.lower(), duration_ms, 0.0

        if "RESULT:FAILED" in output:
            for out_line in output.split("\n"):
                if "RESULT:FAILED" in out_line:
                    reason = (
                        out_line.split("RESULT:FAILED:")[-1].strip()
                        if ":" in out_line[out_line.index("FAILED") + 6:]
                        else "unknown"
                    )
                    reason = _clean_reason(reason)
                    PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue"}
                    if reason in PROMOTE_TO_STATUS:
                        add_event(f"[W{worker_id}] {reason.upper()} ({elapsed}s): {job['title'][:30]}")
                        update_state(worker_id, status=reason,
                                     last_action=f"{reason.upper()} ({elapsed}s)")
                        return reason, duration_ms, 0.0
                    add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:30]}")
                    update_state(worker_id, status="failed",
                                 last_action=f"FAILED: {reason[:25]}")
                    return f"failed:{reason}", duration_ms, 0.0
            return "failed:unknown", duration_ms, 0.0

        add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
        return "failed:no_result_line", duration_ms, 0.0

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
        return "failed:timeout", duration_ms, 0.0
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
        return f"failed:{str(e)[:100]}", duration_ms, 0.0
    finally:
        with _legacy_lock:
            _legacy_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def _run_job_mcp(job: dict, port: int, worker_id: int,
                 model: str, dry_run: bool, agent_prompt: str) -> tuple[str, int, float]:
    """MCP-based apply path (4.5 fallback).

    Runs the same apply logic as the Ollama path, but talks to a Playwright
    MCP server (e.g. @playwright/mcp) instead of a local Python tool runner.
    Activated by setting APPLY_AGENT=mcp in the environment.

    Requirements:
        - APPLY_MCP_SERVER_URL set OR a Playwright MCP server reachable on
          localhost:8931 (default discovery port)
        - Ollama still works (this fallback only changes the browser side)

    Args:
        job: job dict (must have url, title, etc.)
        port: CDP port for the existing Chrome instance
        worker_id: for log naming
        model: Ollama model name (the LLM still uses Ollama)
        dry_run: dry-run flag
        agent_prompt: full task prompt

    Returns:
        (status, duration_ms, cost_usd) — same shape as other apply paths
    """
    from applypilot.apply.mcp_fallback import is_mcp_available, mcp_status, get_mcp_tools

    if not is_mcp_available():
        error_msg = mcp_status()
        logger.error("_run_job_mcp: %s", error_msg)
        add_event(f"[W{worker_id}] MCP unavailable — install @playwright/mcp or set APPLY_MCP_SERVER_URL")
        return "failed:mcp-unavailable", 0, 0.0

    if dry_run:
        return "skipped:dry-run", 0, 0.0

    # Fetch MCP tools and use the Ollama agent loop with MCP tools
    # (instead of the local _PlaywrightToolRunner). This requires a slightly
    # different ollama_agent call signature — for now we just log and warn.
    tools = get_mcp_tools()
    if not tools:
        logger.warning("_run_job_mcp: MCP server reachable but no tools returned")
        return "failed:mcp-no-tools", 0, 0.0

    # ── TODO: implement MCP-based agent loop ──
    # For now, the MCP path is detected + reported but falls back to the
    # Ollama agent with local tools. To fully implement:
    #   1. Add a new run_ollama_agent_with_mcp() function in ollama_agent.py
    #      that takes `mcp_tools` and routes them via the MCP JSON-RPC client
    #   2. Call it from this function with the fetched tools
    # Until that's done, log a clear message and return failure.
    logger.info(
        "_run_job_mcp: MCP server detected with %d tools, but the MCP agent "
        "loop is not yet fully implemented. Falling back to local Ollama agent.",
        len(tools),
    )
    add_event(
        f"[W{worker_id}] MCP detected ({len(tools)} tools) but MCP loop not "
        f"implemented — set APPLY_AGENT=ollama to use local agent"
    )
    return "failed:mcp-not-implemented", 0, 0.0


def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "ollama-default", dry_run: bool = False) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: LLM model name.
        dry_run: Don't click Submit.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0

        chrome_proc = None
        try:
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless)

            result, duration_ms, cost = run_job(job, port=port, worker_id=worker_id,
                                                  model=model, dry_run=dry_run)

            if result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif result == "applied":
                if dry_run:
                    # Dry-run: don't actually mark the job as applied. Use a
                    # dedicated status so the row is preserved but clearly
                    # marked as a practice run.
                    mark_result(
                        job["url"], "dry_run_ok",
                        duration_ms=duration_ms, cost_usd=cost,
                    )
                    add_event(
                        f"[W{worker_id}] DRY-RUN OK ({elapsed}s): {job['title'][:30]}"
                    )
                else:
                    mark_result(
                        job["url"], "applied",
                        duration_ms=duration_ms, cost_usd=cost,
                    )
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms, cost_usd=cost)
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = False, model: str = "ollama-default",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: LLM model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = Console()

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active legacy CLI processes to skip current jobs
            with _legacy_lock:
                for wid, cproc in list(_legacy_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _legacy_lock:
                for wid, cproc in list(_legacy_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                )
            else:
                # Multi-worker — distribute limit across workers
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0)
                              for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
