"""ApplyPilot database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from applypilot.config import DB_PATH, TAILORED_DIR, COVER_LETTER_DIR

log = logging.getLogger(__name__)

# Thread-local connection storage — each thread gets its own connection
# (required for SQLite thread safety with parallel workers)
_local = threading.local()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local cached SQLite connection with WAL mode enabled.

    Each thread gets its own connection (required for SQLite thread safety).
    Connections are cached and reused within the same thread.

    Args:
        db_path: Override the default DB_PATH. Useful for testing.

    Returns:
        sqlite3.Connection configured with WAL mode and row factory.
    """
    path = str(db_path or DB_PATH)

    if not hasattr(_local, 'connections'):
        _local.connections = {}

    conn = _local.connections.get(path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass

    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    _local.connections[path] = conn
    return conn


def close_connection(db_path: Path | str | None = None) -> None:
    """Close the cached connection for the current thread."""
    path = str(db_path or DB_PATH)
    if hasattr(_local, 'connections'):
        conn = _local.connections.pop(path, None)
        if conn is not None:
            conn.close()


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create the full jobs table with all columns from every pipeline stage.

    This is idempotent -- safe to call on every startup. Uses CREATE TABLE IF NOT EXISTS
    so it won't destroy existing data.

    Schema columns by stage:
      - Discovery:  url, title, salary, description, location, site, strategy, discovered_at
      - Enrichment: full_description, application_url, detail_scraped_at, detail_error
      - Scoring:    fit_score, score_reasoning, scored_at
      - Tailoring:  tailored_resume_path, tailored_at, tailor_attempts
      - Cover:      cover_letter_path, cover_letter_at, cover_attempts
      - Apply:      applied_at, apply_status, apply_error, apply_attempts,
                   agent_id, last_attempted_at, apply_duration_ms, apply_task_id,
                   verification_confidence

    Args:
        db_path: Override the default DB_PATH.

    Returns:
        sqlite3.Connection with the schema initialized.
    """
    path = db_path or DB_PATH

    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            -- Discovery stage (smart_extract / job_search)
            url                   TEXT PRIMARY KEY,
            title                 TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            site                  TEXT,
            strategy              TEXT,
            discovered_at         TEXT,

            -- Enrichment stage (detail_scraper)
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,

            -- Scoring stage (job_scorer)
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,

            -- Tailoring stage (resume tailor)
            tailored_resume_path  TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,

            -- Cover letter stage
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,

            -- Application stage
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence TEXT
        )
    """)
    conn.commit()

    # Run migrations for any columns added after initial schema
    ensure_columns(conn)
    ensure_indexes(conn)

    return conn


# Complete column registry: column_name -> SQL type with optional default.
# This is the single source of truth. Adding a column here is all that's needed
# for it to appear in both new databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # Discovery
    "url": "TEXT PRIMARY KEY",
    "title": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "site": "TEXT",
    "strategy": "TEXT",
    "discovered_at": "TEXT",
    # Enrichment
    "full_description": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    # Scoring
    "fit_score": "INTEGER",
    "score_reasoning": "TEXT",
    "scored_at": "TEXT",
    # Tailoring
    "tailored_resume_path": "TEXT",
    "tailored_at": "TEXT",
    "tailor_attempts": "INTEGER DEFAULT 0",
    # Cover letter
    "cover_letter_path": "TEXT",
    "cover_letter_at": "TEXT",
    "cover_attempts": "INTEGER DEFAULT 0",
    # Application
    "applied_at": "TEXT",
    "apply_status": "TEXT",
    "apply_error": "TEXT",
    "apply_attempts": "INTEGER DEFAULT 0",
    "agent_id": "TEXT",
    "last_attempted_at": "TEXT",
    "apply_duration_ms": "INTEGER",
    "apply_task_id": "TEXT",
    "verification_confidence": "TEXT",
    # Estimated LLM cost in USD per apply attempt (populated by Ollama agent).
    "apply_cost_usd": "REAL",
    # User-marked preservation flags. When set, the row is excluded from
    # weekly purge. The user explicitly approved this job for application.
    "approved_at": "TEXT",
}


def ensure_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns to the jobs table (forward migration).

    Reads the current table schema via PRAGMA table_info and compares against
    the full column registry. Any missing columns are added with ALTER TABLE.

    This makes it safe to upgrade the database from any previous version --
    columns are only added, never removed or renamed.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of column names that were added (empty if schema was already current).
    """
    if conn is None:
        conn = get_connection()

    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    added = []

    for col, dtype in _ALL_COLUMNS.items():
        if col not in existing:
            # PRIMARY KEY columns can't be added via ALTER TABLE, but url
            # is always created with the table itself so this is safe
            if "PRIMARY KEY" in dtype:
                continue
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
            added.append(col)

    if added:
        conn.commit()

    return added


# Indexes that significantly speed up stage queries and the daily/weekly
# window filters. All are idempotent (CREATE INDEX IF NOT EXISTS).
_INDEX_DDL: list[str] = [
    # Speeds up --since/older-than-days filters + ORDER BY discovered_at DESC
    "CREATE INDEX IF NOT EXISTS idx_jobs_discovered_at ON jobs(discovered_at)",
    # Speeds up "ready to apply", "ready to tailor", "approved" set queries
    "CREATE INDEX IF NOT EXISTS idx_jobs_apply_status ON jobs(apply_status)",
    # Speeds up the "ready_to_apply" view (tailored + cover + apply_url)
    "CREATE INDEX IF NOT EXISTS idx_jobs_fit_score ON jobs(fit_score)",
]


def ensure_indexes(conn: sqlite3.Connection | None = None) -> list[str]:
    """Create the standard set of indexes (idempotent).

    Returns the list of index names that were created in this call.
    """
    if conn is None:
        conn = get_connection()
    created = []
    for ddl in _INDEX_DDL:
        # Extract the index name from the DDL for reporting
        try:
            name = ddl.split("IF NOT EXISTS ")[1].split(" ")[0]
        except IndexError:
            name = "?"
        # Check existence via sqlite_master
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
        if not exists:
            conn.execute(ddl)
            created.append(name)
    if created:
        conn.commit()
    return created


def get_stats(conn: sqlite3.Connection | None = None) -> dict:
    """Return job counts by pipeline stage.

    Provides a snapshot of how many jobs are at each stage, useful for
    dashboard display and pipeline progress tracking.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        Dictionary with keys:
            total, by_site, pending_detail, with_description,
            scored, unscored, tailored, untailored_eligible,
            with_cover_letter, applied, score_distribution
    """
    if conn is None:
        conn = get_connection()

    stats: dict = {}

    # Total jobs
    stats["total"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # By site breakdown
    rows = conn.execute(
        "SELECT site, COUNT(*) as cnt FROM jobs GROUP BY site ORDER BY cnt DESC"
    ).fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    # Enrichment stage
    stats["pending_detail"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL"
    ).fetchone()[0]

    stats["with_description"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL"
    ).fetchone()[0]

    stats["detail_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL"
    ).fetchone()[0]

    # Scoring stage
    stats["scored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND fit_score IS NULL"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    # Tailoring stage
    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL"
    ).fetchone()[0]

    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score >= 7 AND full_description IS NOT NULL "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(tailor_attempts, 0) >= 5 "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= 5 "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '')"
    ).fetchone()[0]

    # Application stage
    stats["applied"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()[0]

    stats["apply_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL"
    ).fetchone()[0]

    stats["ready_to_apply"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND applied_at IS NULL "
        "AND application_url IS NOT NULL"
    ).fetchone()[0]

    return stats


# -------------------------------------------------------------------
# Per-site success rate tracking (4.9) + dynamic blacklist (4.10)
# -------------------------------------------------------------------

def get_site_stats(conn: sqlite3.Connection | None = None,
                   days: int = 30,
                   min_attempts: int = 3) -> list[dict]:
    """Per-site apply success rate over the last N days.

    A site is included only if it has at least `min_attempts` apply attempts
    in the window (so we don't act on a 1/1 fluke). Sites with high failure
    rates will be flagged for the dynamic blacklist.

    Args:
        conn: Database connection. Uses get_connection() if None.
        days: Look at apply attempts in the last N days. Default 30.
        min_attempts: Minimum number of attempts to be included. Default 3.

    Returns:
        List of dicts, one per site, sorted by failure_rate descending:
            site, attempts, applied, failed, expired, captcha, login_issue,
            dry_run, success_rate, failure_rate, recent_failure_streak
        Where:
            - attempts = total non-pending apply_status transitions in window
            - success_rate = applied / attempts
            - failure_rate = 1 - success_rate
            - recent_failure_streak = consecutive failures at the end
              (most recent apply_status values, looking backwards)
    """
    if conn is None:
        conn = get_connection()
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Aggregate counts per site
    rows = conn.execute("""
        SELECT
            site,
            COUNT(*) as attempts,
            SUM(CASE WHEN apply_status = 'applied' THEN 1 ELSE 0 END) as applied,
            SUM(CASE WHEN apply_status = 'failed' THEN 1 ELSE 0 END) as failed,
            SUM(CASE WHEN apply_status = 'expired' THEN 1 ELSE 0 END) as expired,
            SUM(CASE WHEN apply_status = 'captcha' THEN 1 ELSE 0 END) as captcha,
            SUM(CASE WHEN apply_status = 'login_issue' THEN 1 ELSE 0 END) as login_issue,
            SUM(CASE WHEN apply_status = 'dry_run_ok' THEN 1 ELSE 0 END) as dry_run
        FROM jobs
        WHERE (applied_at >= ? OR apply_error IS NOT NULL)
          AND site IS NOT NULL
        GROUP BY site
        HAVING attempts >= ?
        ORDER BY attempts DESC
    """, (cutoff_iso, min_attempts)).fetchall()

    results = []
    for row in rows:
        site, attempts, applied, failed, expired, captcha, login_issue, dry_run = row
        attempts = attempts or 0
        applied = applied or 0
        non_dry = max(attempts - (dry_run or 0), 1)
        success_rate = applied / non_dry if non_dry else 0.0
        failure_rate = 1.0 - success_rate

        # Compute recent failure streak (last 5 apply attempts for this site)
        recent = conn.execute("""
            SELECT apply_status FROM jobs
            WHERE site = ? AND (applied_at IS NOT NULL OR apply_error IS NOT NULL)
              AND applied_at >= ?
            ORDER BY COALESCE(applied_at, '') DESC
            LIMIT 5
        """, (site, cutoff_iso)).fetchall()
        streak = 0
        for (status,) in recent:
            if status in ("failed", "expired", "captcha", "login_issue"):
                streak += 1
            else:
                break

        results.append({
            "site": site,
            "attempts": attempts,
            "applied": applied,
            "failed": failed or 0,
            "expired": expired or 0,
            "captcha": captcha or 0,
            "login_issue": login_issue or 0,
            "dry_run": dry_run or 0,
            "success_rate": round(success_rate, 3),
            "failure_rate": round(failure_rate, 3),
            "recent_failure_streak": streak,
        })

    # Sort by failure_rate descending (most-broken sites first)
    results.sort(key=lambda r: r["failure_rate"], reverse=True)
    return results


def get_dynamic_blacklist(conn: sqlite3.Connection | None = None,
                           days: int = 30,
                           min_attempts: int = 3,
                           failure_threshold: float = 0.85,
                           streak_threshold: int = 3) -> list[dict]:
    """Compute the dynamic blacklist: sites to auto-skip on the next apply.

    A site is blacklisted if BOTH conditions hold:
      1. failure_rate > failure_threshold (default 85%) in the last N days
      2. recent_failure_streak >= streak_threshold (default 3)

    Env var overrides (read on every call, no need to restart):
      APPLY_BLACKLIST_FAILURE_THRESHOLD  (default 0.85)
      APPLY_BLACKLIST_STREAK_THRESHOLD   (default 3)
      APPLY_BLACKLIST_DAYS               (default 30)
      APPLY_BLACKLIST_MIN_ATTEMPTS       (default 3)

    Args:
        conn: Database connection.
        days: Look at the last N days. Default 30.
        min_attempts: Minimum attempts to be considered. Default 3.
        failure_threshold: Maximum allowed failure_rate. Default 0.85.
        streak_threshold: Maximum allowed recent failure streak. Default 3.

    Returns:
        List of dicts (subset of get_site_stats output) for blacklisted sites.
    """
    # Read env-var overrides (cheap; only the keys we know about)
    try:
        failure_threshold = float(os.environ.get("APPLY_BLACKLIST_FAILURE_THRESHOLD", failure_threshold))
    except (TypeError, ValueError):
        pass
    try:
        streak_threshold = int(os.environ.get("APPLY_BLACKLIST_STREAK_THRESHOLD", streak_threshold))
    except (TypeError, ValueError):
        pass
    try:
        days = int(os.environ.get("APPLY_BLACKLIST_DAYS", days))
    except (TypeError, ValueError):
        pass
    try:
        min_attempts = int(os.environ.get("APPLY_BLACKLIST_MIN_ATTEMPTS", min_attempts))
    except (TypeError, ValueError):
        pass

    stats = get_site_stats(conn=conn, days=days, min_attempts=min_attempts)
    return [
        s for s in stats
        if s["failure_rate"] > failure_threshold and s["recent_failure_streak"] >= streak_threshold
    ]


def is_site_blacklisted(site: str | None,
                        conn: sqlite3.Connection | None = None,
                        days: int = 30,
                        min_attempts: int = 3,
                        failure_threshold: float = 0.85,
                        streak_threshold: int = 3) -> tuple[bool, str]:
    """Check if a specific site is currently on the dynamic blacklist.

    Args:
        site: Site name to check (e.g. 'linkedin', 'indeed').
        conn: Database connection.
        ... (same thresholds as get_dynamic_blacklist)

    Returns:
        (is_blacklisted, reason). If is_blacklisted is False, reason is "".
    """
    if not site:
        return False, ""
    blacklist = get_dynamic_blacklist(
        conn=conn, days=days, min_attempts=min_attempts,
        failure_threshold=failure_threshold, streak_threshold=streak_threshold,
    )
    for entry in blacklist:
        if entry["site"] == site:
            reason = (
                f"site blacklisted: {site} has {entry['failure_rate']*100:.0f}% failure rate "
                f"and {entry['recent_failure_streak']}-streak failures over the last {days} days "
                f"({entry['attempts']} attempts, {entry['applied']} applied)"
            )
            return True, reason
    return False, ""


def get_blacklist_as_dict(conn: sqlite3.Connection | None = None) -> dict[str, str]:
    """Return the dynamic blacklist as a {site: reason} dict.

    Convenience for the Streamlit dashboard or any consumer that just wants
    a quick lookup.
    """
    blacklist = get_dynamic_blacklist(conn=conn)
    return {entry["site"]: f"{entry['failure_rate']*100:.0f}% failure ({entry['attempts']} attempts)"
            for entry in blacklist}


def _canonicalize_url(url: str) -> str:
    """Normalize a job URL to its canonical form to reduce duplicate tracking.

    Strips common tracking query params: utm_*, ref, refId, trk, vjk, gclid, fbclid.
    Keeps the core path and essential params (job id).

    LinkedIn example:
        https://www.linkedin.com/jobs/view/4417252427/?trk=public_jobs_job-result
        -> https://www.linkedin.com/jobs/view/4417252427

    Indeed example:
        https://www.indeed.com/viewjob?jk=abc123&vjk=xyz456&fromage=1
        -> https://www.indeed.com/viewjob?jk=abc123
    """
    if not url:
        return url
    from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
    try:
        p = urlparse(url)
    except ValueError:
        return url

    TRACKING = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "ref", "refId", "trk", "trackingId", "gclid", "fbclid", "msclkid",
        "_ga", "_gl", "vero_id", "vero_conv", "mc_cid", "mc_eid",
        "fromage",  # Indeed: days ago, varies per query
        "from", "vjk",  # LinkedIn/Indeed tracking
    }
    # Always keep these Indeed/LinkedIn-essential params
    ESSENTIAL = {"jk", "currentJobId", "q", "pageNum"}

    if p.query:
        params = parse_qs(p.query, keep_blank_values=True)
        filtered = {k: v for k, v in params.items()
                    if k not in TRACKING or k in ESSENTIAL}
        new_q = urlencode(filtered, doseq=True) if filtered else ""
    else:
        new_q = ""

    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), p.params, new_q, ""))


def store_jobs(conn: sqlite3.Connection, jobs: list[dict],
               site: str, strategy: str) -> tuple[int, int]:
    """Store discovered jobs, skipping duplicates by URL.

    URLs are canonicalized (tracking query params stripped) before storage
    to deduplicate common cases like LinkedIn `?trk=` and Indeed `?vjk=`.

    Args:
        conn: Database connection.
        jobs: List of job dicts with keys: url, title, salary, description, location.
        site: Source site name (e.g. "RemoteOK", "Dice").
        strategy: Extraction strategy used (e.g. "json_ld", "api_response", "css_selectors").

    Returns:
        Tuple of (new_count, duplicate_count).
    """
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0
    seen_in_batch: set[str] = set()

    for job in jobs:
        raw_url = job.get("url")
        if not raw_url:
            continue
        url = _canonicalize_url(raw_url)
        if url in seen_in_batch:
            existing += 1
            continue
        seen_in_batch.add(url)
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), job.get("salary"), job.get("description"),
                 job.get("location"), site, strategy, now),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


def get_jobs_by_stage(conn: sqlite3.Connection | None = None,
                     stage: str = "discovered",
                     min_score: int | None = None,
                     limit: int = 100,
                     since: str | None = None) -> list[dict]:
    """Fetch jobs filtered by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: One of "discovered", "enriched", "scored", "tailored", "applied".
        min_score: Minimum fit_score filter (only relevant for scored+ stages).
        limit: Maximum number of rows to return.
        since: ISO-8601 datetime string. If set, only jobs with
               `discovered_at >= since` are returned. Used by daily cron to
               restrict processing to a recent window (e.g. last 24h).

    Returns:
        List of job dicts.
    """
    if conn is None:
        conn = get_connection()

    conditions = {
        "discovered": "1=1",
        "pending_detail": "detail_scraped_at IS NULL",
        "enriched": "full_description IS NOT NULL",
        "pending_score": "full_description IS NOT NULL AND fit_score IS NULL",
        "scored": "fit_score IS NOT NULL",
        "pending_tailor": (
            "fit_score >= ? AND full_description IS NOT NULL "
            "AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < 5"
        ),
        "tailored": "tailored_resume_path IS NOT NULL",
        "pending_apply": (
            "tailored_resume_path IS NOT NULL AND applied_at IS NULL "
            "AND application_url IS NOT NULL"
        ),
        "applied": "applied_at IS NOT NULL",
    }

    where = conditions.get(stage, "1=1")
    params: list = []

    if "?" in where and min_score is not None:
        params.append(min_score)
    elif "?" in where:
        params.append(7)  # default min_score

    if min_score is not None and "fit_score" not in where and stage in ("scored", "tailored", "applied"):
        where += " AND fit_score >= ?"
        params.append(min_score)

    # Optional date window — only include jobs discovered at/after `since`.
    # Skip on stage="discovered" because that's a discovery-status filter
    # independent of recency. (Discovery itself happens during the cron run.)
    if since is not None and stage != "discovered":
        where += " AND discovered_at >= ?"
        params.append(since)

    query = f"SELECT * FROM jobs WHERE {where} ORDER BY fit_score DESC NULLS LAST, discovered_at DESC"
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()

    # Convert sqlite3.Row objects to dicts
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []


def purge_old_jobs(
    older_than_days: int = 7,
    dry_run: bool = False,
    preserve_applied: bool = True,
    preserve_approved: bool = True,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Delete jobs discovered more than `older_than_days` ago, plus their files.

    Removes:
      - Tailored resume files referenced by `tailored_resume_path`
      - Cover letter files referenced by `cover_letter_path`
      - The DB row itself

    Preserves (by default):
      - Jobs you've already applied to (`applied_at IS NOT NULL`)
      - Jobs you've explicitly approved for application (`approved_at IS NOT NULL`)

    Args:
        older_than_days: Cutoff in days. Jobs with `discovered_at` strictly
                         older than `now - older_than_days` are purged.
        dry_run: If True, return counts and file lists without deleting.
        preserve_applied: If True (default), jobs with `applied_at IS NOT NULL`
                          are kept regardless of age.
        preserve_approved: If True (default), jobs with `approved_at IS NOT NULL`
                           are kept regardless of age (the user's "approved" set).
        conn: Optional DB connection.

    Returns:
        {"purged": int, "files_deleted": int, "files_missing": int,
         "preserved_applied": int, "preserved_approved": int, "dry_run": bool}
    """
    if conn is None:
        conn = get_connection()

    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()

    where = "discovered_at < ?"
    params: list = [cutoff]
    if preserve_applied:
        where += " AND applied_at IS NULL"
    if preserve_approved:
        where += " AND approved_at IS NULL"

    rows = conn.execute(f"SELECT * FROM jobs WHERE {where}", params).fetchall()
    if not rows:
        # Compute preserved counts even when there's nothing to purge.
        preserved_applied = preserved_approved = 0
        if preserve_applied:
            preserved_applied = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE discovered_at < ? AND applied_at IS NOT NULL",
                (cutoff,),
            ).fetchone()[0]
        if preserve_approved:
            preserved_approved = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE discovered_at < ? AND approved_at IS NOT NULL",
                (cutoff,),
            ).fetchone()[0]
        return {
            "purged": 0, "files_deleted": 0, "files_missing": 0,
            "preserved_applied": preserved_applied,
            "preserved_approved": preserved_approved,
            "dry_run": dry_run,
        }

    files_to_delete: list[Path] = []
    urls_to_delete: list[str] = []
    for row in rows:
        d = dict(row)
        urls_to_delete.append(d["url"])
        for key in ("tailored_resume_path", "cover_letter_path"):
            path_str = d.get(key)
            if not path_str:
                continue
            p = Path(path_str)
            # Safety: only delete if the file lives inside our APP_DIR
            try:
                p_resolved = p.resolve()
            except (OSError, RuntimeError):
                continue
            if TAILORED_DIR.resolve() in p_resolved.parents or \
               COVER_LETTER_DIR.resolve() in p_resolved.parents or \
               p_resolved.parent == TAILORED_DIR.resolve() or \
               p_resolved.parent == COVER_LETTER_DIR.resolve():
                files_to_delete.append(p)
            # else: outside our managed dirs — skip silently

    preserved_applied = preserved_approved = 0
    if preserve_applied:
        preserved_applied = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE discovered_at < ? AND applied_at IS NOT NULL",
            (cutoff,),
        ).fetchone()[0]
    if preserve_approved:
        preserved_approved = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE discovered_at < ? AND approved_at IS NOT NULL",
            (cutoff,),
        ).fetchone()[0]

    if dry_run:
        return {
            "purged": 0, "files_deleted": 0, "files_missing": 0,
            "files_would_delete": len(files_to_delete),
            "rows_would_delete": len(urls_to_delete),
            "preserved_applied": preserved_applied,
            "preserved_approved": preserved_approved,
            "dry_run": True,
        }

    # Delete files first (best-effort; missing files are not an error)
    files_deleted = 0
    files_missing = 0
    for p in files_to_delete:
        try:
            p.unlink()
            files_deleted += 1
        except FileNotFoundError:
            files_missing += 1
        except OSError as exc:
            log.warning("Failed to delete %s: %s", p, exc)

    # Delete DB rows in a single transaction
    purged = 0
    if urls_to_delete:
        placeholders = ",".join("?" * len(urls_to_delete))
        cur = conn.execute(
            f"DELETE FROM jobs WHERE url IN ({placeholders})",
            urls_to_delete,
        )
        purged = cur.rowcount
        conn.commit()

    return {
        "purged": purged,
        "files_deleted": files_deleted,
        "files_missing": files_missing,
        "preserved_applied": preserved_applied,
        "preserved_approved": preserved_approved,
        "dry_run": False,
    }
