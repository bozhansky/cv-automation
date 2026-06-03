"""
ApplyPilot Web UI
Single-file Streamlit frontend for the job application pipeline.
Navigate: sidebar selects page, query_params handle job deep-links.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

import streamlit as st

# ── Tools ─────────────────────────────────────────────────────────────────────
# cronjob from hermes_tools (available in agent context)
try:
    from hermes_tools import cronjob as _cronjob
    def cronjob_safe(**kw):
        try:
            return _cronjob(**kw)
        except Exception:
            return {"jobs": []}
except ImportError:
    def cronjob_safe(**kw):
        return {"jobs": []}

# ── Paths ────────────────────────────────────────────────────────────────────
APPLYPILOT_DIR = Path.home() / ".applypilot"
DB_PATH        = APPLYPILOT_DIR / "applypilot.db"
RESUME_PATH    = APPLYPILOT_DIR / "resume.txt"
TAILORED_DIR   = APPLYPILOT_DIR / "tailored_resumes"
COVER_DIR      = APPLYPILOT_DIR / "cover_letters"
PROFILE_PATH   = APPLYPILOT_DIR / "profile.json"
SEARCHES_PATH  = APPLYPILOT_DIR / "searches.yaml"
AUTOSEARCH_PATH = APPLYPILOT_DIR / "autosearch.json"

# ── Auto-search persistence (works outside Hermes) ─────────────────────────────
def load_autosearch():
    """Load auto-search config, checking system crontab as source of truth"""
    cfg = {"enabled": False, "interval_min": 30}
    
    # Check system crontab for applypilot discover job
    try:
        import subprocess
        crontab = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if crontab.returncode == 0:
            for line in crontab.stdout.split('\n'):
                if 'applypilot' in line.lower() and 'discover' in line.lower():
                    cfg["enabled"] = True
                    # Extract interval (e.g., */30 or 0)
                    if line.startswith('*/'):
                        try:
                            cfg["interval_min"] = int(line.split('/')[1].split()[0])
                        except (IndexError, ValueError):
                            pass
                    break
    except Exception:
        pass
    
    # Merge with saved config if exists (user's toggle preference)
    if AUTOSEARCH_PATH.exists():
        try:
            saved = json.loads(AUTOSEARCH_PATH.read_text())
            cfg.update(saved)
            # System crontab overrides file 'enabled' - it's the source of truth
            # but we keep the file's interval_min for UI display
        except Exception:
            pass
    
    return cfg

def save_autosearch(cfg: dict):
    AUTOSEARCH_PATH.write_text(json.dumps(cfg))

def fmt_discovered(val):
    """Format discovered_at date for display"""
    if not val:
        return ""
    try:
        dt = datetime.fromisoformat(str(val).replace('Z', '+'))
        return dt.strftime("%b %d")  # "May 28"
    except Exception:
        return str(val)[:10]


def fmt_date_full(val) -> str:
    """Format any date column for display. Returns 'May 28, 14:30' or '—' if None."""
    if not val:
        return "—"
    try:
        dt = datetime.fromisoformat(str(val).replace('Z', '+'))
        return dt.strftime("%b %d, %H:%M")
    except Exception:
        return str(val)[:16]


def fmt_date_only(val) -> str:
    """Format any date column as YYYY-MM-DD for date inputs / filtering."""
    if not val:
        return ""
    try:
        dt = datetime.fromisoformat(str(val).replace('Z', '+'))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return str(val)[:10]


def _parse_iso_date(s: str | None):
    """Parse YYYY-MM-DD string into datetime (or None). Used by date filters."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def get_filtered_jobs(
    site: str | None = None,
    title_contains: str | None = None,
    discovered_from: str | None = None,
    discovered_to: str | None = None,
    score_min: float | None = None,
    score_max: float | None = None,
    applied_from: str | None = None,
    applied_to: str | None = None,
    applied_only: bool = False,
    limit: int = 500,
) -> list[dict]:
    """Return jobs matching all the given filters. Empty filter = no constraint.

    Args:
        site: Exact site match (e.g. 'linkedin')
        title_contains: Case-insensitive substring match on title
        discovered_from / discovered_to: YYYY-MM-DD inclusive bounds on discovered_at
        score_min / score_max: Inclusive bounds on fit_score (NULLs are kept)
        applied_from / applied_to: YYYY-MM-DD inclusive bounds on applied_at
        applied_only: If True, restrict to jobs with applied_at NOT NULL
        limit: Max rows to return

    Returns:
        List of job dicts ordered by fit_score DESC, discovered_at DESC.
    """
    conn = get_conn()
    where = []
    args: list = []
    if site and site != "(all)":
        where.append("site = ?")
        args.append(site)
    if title_contains:
        where.append("LOWER(title) LIKE ?")
        args.append(f"%{title_contains.lower()}%")
    df = _parse_iso_date(discovered_from)
    if df:
        where.append("discovered_at >= ?")
        args.append(df.isoformat())
    dt = _parse_iso_date(discovered_to)
    if dt:
        # inclusive — bump by 1 day and use < so the whole day is included
        from datetime import timedelta as _td
        where.append("discovered_at < ?")
        args.append((dt + _td(days=1)).isoformat())
    if score_min is not None:
        where.append("fit_score >= ?")
        args.append(score_min)
    if score_max is not None:
        where.append("fit_score <= ?")
        args.append(score_max)
    af = _parse_iso_date(applied_from)
    if af:
        where.append("applied_at >= ?")
        args.append(af.isoformat())
    at = _parse_iso_date(applied_to)
    if at:
        from datetime import timedelta as _td
        where.append("applied_at < ?")
        args.append((at + _td(days=1)).isoformat())
    if applied_only:
        where.append("applied_at IS NOT NULL")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM jobs {where_sql} ORDER BY fit_score DESC NULLS LAST, "
        f"discovered_at DESC NULLS LAST LIMIT ?",
        (*args, limit),
    ).fetchall()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    return [dict(zip(cols, r)) for r in rows]


def delete_job_by_url(url: str) -> tuple[bool, str]:
    """Delete a job from the DB. Also tries to delete its tailored/cover files
    from disk. Returns (success, message)."""
    if not url:
        return False, "No URL provided"
    conn = get_conn()
    row = conn.execute(
        "SELECT tailored_resume_path, cover_letter_path FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        return False, "Job not found in DB"
    files_deleted = []
    for path_str in row:
        if path_str:
            try:
                p = Path(path_str)
                if p.exists() and p.is_file():
                    p.unlink()
                    files_deleted.append(str(p))
            except OSError as e:
                # Don't fail the delete just because file removal failed
                pass
    conn.execute("DELETE FROM jobs WHERE url = ?", (url,))
    conn.commit()
    msg = f"Deleted job {url[:60]}..."
    if files_deleted:
        msg += f" (+ {len(files_deleted)} file(s))"
    return True, msg

# ── Add project agents/ to path for auto_apply module ─────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# ── DB cursor helper ──────────────────────────────────────────────────────────
def get_conn():
    return sqlite3.connect(str(DB_PATH), timeout=30)

def _count_jobs_above_score(min_score: int) -> int:
    """Count jobs in the DB with fit_score >= min_score. Used by pipeline UI."""
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE fit_score >= ?", (min_score,)
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0

def row_to_dict(row) -> dict | None:
    if row is None:
        return None
    conn = get_conn()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    return dict(zip(cols, row))

def job_by_url(url: str) -> dict | None:
    conn = get_conn()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    return dict(zip(cols, row)) if row else None

def all_jobs(sort_by="score") -> list[dict]:
    conn = get_conn()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if sort_by == "date":
        order = "discovered_at DESC NULLS LAST, fit_score DESC"
    else:  # default score
        order = "fit_score DESC NULLS LAST, discovered_at DESC"
    rows = conn.execute(
        f"SELECT * FROM jobs ORDER BY {order} LIMIT 500"
    ).fetchall()
    if not rows:
        return []
    return [dict(zip(cols, r)) for r in rows]

# ── Helpers ──────────────────────────────────────────────────────────────────
def score_badge(score):
    if score is None:
        return "⚪ No score"
    if score >= 8:
        return f"🟢 {score}"
    if score >= 6:
        return f"🟡 {score}"
    return f"🔴 {score}"

def read_file_text(path_str: str | None) -> str:
    if not path_str:
        return ""
    p = Path(path_str)
    if p.exists():
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
    return ""

def run_stage(stage: str, timeout=600, min_score: int = 0,
              since: str | None = None, workers: int = 1) -> tuple[str, int]:
    """Run applypilot stage. Returns (output, returncode).

    Args:
        stage: Pipeline stage name (or 'cover_sl' for multilingual).
        timeout: Max seconds to wait.
        min_score: Minimum fit score for tailor/cover (0 = no filter).
        since: Optional ISO datetime or relative string ('24h', '7d').
        workers: Number of parallel workers.
    """
    env = os.environ.copy()
    env["HOME"] = str(Path.home())

    # Handle custom stages
    if stage == "cover_sl":
        # Run Slovenian cover letter generation
        res = subprocess.run(
            ["python3", str(Path(__file__).parent.parent / "agents" / "cover_letter_multilingual.py")],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(Path.home()), env=env,
        )
        return res.stdout + res.stderr, res.returncode

    # Build CLI args for the standard applypilot stages
    args = ["python3", "-m", "applypilot", "run", stage]
    if min_score and min_score > 0:
        args += ["--min-score", str(min_score)]
    if since:
        args += ["--since", since]
    if workers and workers > 1:
        args += ["--workers", str(workers)]

    res = subprocess.run(
        args,
        capture_output=True, text=True, timeout=timeout,
        cwd=str(Path.home()), env=env,
    )
    return res.stdout + res.stderr, res.returncode

def save_job_apply_status(url: str, status: str):
    conn = get_conn()
    conn.execute(
        "UPDATE jobs SET apply_status = ?, applied_at = ? WHERE url = ?",
        (status, datetime.utcnow().isoformat(), url)
    )
    conn.commit()

def _parse_cron_interval(schedule: str | None) -> int:
    """Parse interval in minutes from a cron schedule string like '30m' or '2h'."""
    if not schedule:
        return 30
    s = schedule.strip().lower()
    if s.endswith('m'):
        return int(s[:-1])
    if s.endswith('h'):
        return int(s[:-1]) * 60
    return 30

# ── Page config ──────────────────────────────────────────────────────────────
PAGES = ["Dashboard", "Jobs", "Job Detail", "Pipeline", "Site Analytics", "Settings"]

def _set_page(page: str):
    st.query_params["page"] = page
    st.query_params["job"] = ""

def _current_page() -> str:
    p = st.query_params.get("page")
    return p if p in PAGES else "Dashboard"

def _current_job() -> str | None:
    raw = st.query_params.get("job")
    if raw:
        try:
            return urllib.parse.unquote(raw)
        except Exception:
            return raw
    return None

# Bootstrap: if a job param is set, record it and switch to Job Detail
_job_url = _current_job()
if _job_url and _current_page() != "Job Detail":
    _set_page("Job Detail")

# ── Bootstrap guard — only run on user-initiated reruns ────────────────────────
# Prevents DeltaGenerator artifact from appearing when rerun fires during element construction.
if "rerun_guard" not in st.session_state:
    st.session_state["rerun_guard"] = True
else:
    # Already initialized — this is a re-render from a widget action.
    # DeltaGenerator artifacts appear when st.rerun() fires during construction.
    pass

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ApplyPilot",
    page_icon="🎯",
    layout="wide",
)

# ── Sidebar navigation ────────────────────────────────────────────────────────
st.sidebar.title("🎯 ApplyPilot")
selection = st.sidebar.radio("Navigate", PAGES, index=PAGES.index(_current_page()))

# ── Pages ────────────────────────────────────────────────────────────────────
def _fetch_linkedin_profile() -> dict:
    """Scrape LinkedIn profile using Chrome CDP with li_at cookie."""
    import httpx
    try:
        import websocket  # noqa: F401
    except ImportError:
        return {"error": "websocket-client not installed. Run: pip3 install websocket-client --break-system-packages"}

    import json, time, re
    from pathlib import Path

    APP_DIR = Path.home() / ".applypilot"
    CHROME_WORKER = APP_DIR / "chrome-workers" / "worker-0"
    COOKIE_FILE = CHROME_WORKER / "Default" / "Cookies"
    PROFILE_URL = "https://www.linkedin.com/in/spisek-bostjan"  # adjust as needed

    if not COOKIE_FILE.exists():
        return {"error": "Chrome cookies not found. Run the Chrome setup first."}

    try:
        import websocket
        ws = websocket.create_connection(
            "ws://localhost:9222/devtools/browser",
            timeout=10,
        )
    except Exception:
        return {"error": "Cannot connect to Chrome CDP. Is Chrome running with --remote-debugging-port=9222?"}

    # Navigate to LinkedIn
    ws.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": PROFILE_URL}}))
    time.sleep(4)

    # Get document
    ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {"expression": "document.documentElement.outerHTML"}}))
    result = json.loads(ws.recv())
    html = result.get("result", {}).get("result", {}).get("value", "")[:8000]
    ws.close()

    # Extract key sections with regex
    data = {}
    headline_match = re.search(r'"headline":"([^"]+)"', html)
    if headline_match:
        data["headline"] = headline_match.group(1)
    name_match = re.search(r'"firstName":"([^"]+)".*?"lastName":"([^"]+)"', html)
    if name_match:
        data["name"] = f"{name_match.group(1)} {name_match.group(2)}"
    about_match = re.search(r'about[^<]{0,200}<p[^>]*>([^<]+)</p>', html, re.DOTALL)
    if about_match:
        data["about"] = about_match.group(1)[:500]
    data["raw_length"] = len(html)
    return data


def page_dashboard():
    # ── Auto-search controls (top of dashboard) ──────────────────────────
    st.subheader("🔄 Auto-Search")
    ast = load_autosearch()

    cron_jobs = cronjob_safe(action='list').get("jobs", [])
    has_cron = len(cron_jobs) > 0  # Can use Hermes cron if available
    auto_job = next((j for j in cron_jobs if j.get("name") == "Auto Job Discovery"), None)

    c_search, c_toggle, c_interval, c_btn = st.columns([2, 1, 1, 1])

    with c_search:
        mins = ast.get("interval_min", 30)
        st.caption(f"Current: every {mins}m" if ast.get("enabled") else "Off")

    with c_toggle:
        enabled = st.checkbox(
            "Auto-Search", value=ast.get("enabled", False),
            key="auto_search_toggle",
        )

    with c_interval:
        interval = st.number_input(
            "Interval (min)", min_value=5, max_value=480,
            value=mins,
            step=5,
            key="auto_search_interval",
            disabled=not enabled,
        )

    with c_btn:
        st.write("")  # spacer
        if st.button("💾 Save", key="save_auto_search", disabled=not enabled):
            save_autosearch({"enabled": True, "interval_min": int(interval)})
            # Also try to create Hermes cron job if available
            if has_cron:
                if auto_job:
                    if _parse_cron_interval(auto_job.get("schedule")) != interval:
                        cronjob_safe(action='update', job_id=auto_job["job_id"], schedule=f"{interval}m")
                else:
                    cronjob_safe(
                        action='create', name="Auto Job Discovery",
                        prompt="Run: export HOME=/home/bostjan && python3 -m applypilot run discover",
                        schedule=f"{interval}m",
                    )
            st.rerun()

    if not enabled and ast.get("enabled"):
        save_autosearch({"enabled": False, "interval_min": ast.get("interval_min", 30)})
        st.rerun()

    # ── Check for new jobs (manual trigger) ────────────────────────────────
    c_check, c_status = st.columns([1, 4])
    with c_check:
        if st.button("🔍 Check for New Jobs", type="primary", key="check_new_jobs"):
            with st.spinner("Running discover stage…"):
                output, rc = run_stage("discover", timeout=300)
            st.session_state["last_discover_rc"] = rc
            st.session_state["last_discover_out"] = output[-3000:]
            st.rerun()

    with c_status:
        if "last_discover_rc" in st.session_state:
            rc = st.session_state["last_discover_rc"]
            out = st.session_state.get("last_discover_out", "")
            if rc == 0:
                st.success("✅ Discover completed")
            else:
                st.error(f"❌ Discover failed (exit {rc})")
                with st.expander("Output"):
                    st.code(out)

    st.divider()
    st.title("📊 Pipeline Dashboard")

    conn = get_conn()
    total    = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    scored   = conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL").fetchone()[0]
    tailored = conn.execute("SELECT COUNT(*) FROM jobs WHERE tailored_at IS NOT NULL").fetchone()[0]
    cover    = conn.execute("SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL").fetchone()[0]
    applied  = conn.execute("SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL").fetchone()[0]
    ready    = conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score >= 7").fetchone()[0]

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Jobs", total)
    c2.metric("Scored", scored)
    c3.metric("Tailored", tailored)
    c4.metric("Cover Letters", cover)
    c5.metric("Ready to Apply", ready)
    c6.metric("Applied", applied)

    st.divider()

    # Score distribution
    rows = conn.execute(
        "SELECT fit_score, COUNT(*) FROM jobs WHERE fit_score IS NOT NULL GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    if rows:
        chart_data = {"Score": [str(r[0]) for r in rows], "Count": [r[1] for r in rows]}
        st.bar_chart(chart_data, x="Score", y="Count")

    st.divider()
    st.subheader("⏳ Pending Your Approval")
    if st.button("🔄 Refresh Data"):
        st.cache_data.clear()
        st.rerun()
    pending = conn.execute("""
        SELECT * FROM jobs WHERE apply_status = 'pending_approval' ORDER BY fit_score DESC LIMIT 5
    """).fetchall()
    if pending:
        pending_dicts = [row_to_dict(r) for r in pending]
        for job in pending_dicts:
            with st.container():
                c1, c2, c3 = st.columns([1, 5, 1])
                c1.write(score_badge(job.get("fit_score")))
                c2.markdown(f"**{job.get('title', '?')}**\n{job.get('site', '')} · {job.get('location', '')}")
                uk = hashlib.sha1((job.get("url") or "").encode()).hexdigest()[:8]
                # Approve flow: click button → confirm dialog → call mark_approval_approved
                approve_key = f"pend_appr_state_{uk}"
                if approve_key not in st.session_state:
                    st.session_state[approve_key] = "idle"
                aks = st.session_state[approve_key]
                if aks == "idle":
                    if c3.button("Approve", key=f"pend_approve_{uk}"):
                        st.session_state[approve_key] = "confirming"
                        st.rerun()
                elif aks == "confirming":
                    c3.markdown("**Confirm?**")
                    cba, cbb = c3.columns(2)
                    if cba.button("✓ Yes", key=f"pend_yes_{uk}", type="primary"):
                        from agents.auto_apply import mark_approval_approved
                        conn2 = get_conn()
                        mark_approval_approved(conn2, job.get("url"), actor="dashboard")
                        st.session_state[approve_key] = "idle"
                        st.success("Approved ✅")
                        st.rerun()
                    if cbb.button("✗ No", key=f"pend_no_{uk}"):
                        st.session_state[approve_key] = "idle"
                        st.rerun()
                if c3.button("Decline", key=f"pend_decline_{uk}"):
                    from agents.auto_apply import mark_approval_declined
                    conn2 = get_conn()
                    mark_approval_declined(conn2, job.get("url"))
                    st.rerun()
                st.divider()
    else:
        st.caption("No pending approvals.")

    st.divider()
    st.subheader("🗂️ Jobs (filtered, deletable)")

    # ── Filters ─────────────────────────────────────────────────────────
    conn = get_conn()
    all_sites = [r[0] for r in conn.execute(
        "SELECT DISTINCT site FROM jobs WHERE site IS NOT NULL AND site != '' ORDER BY site"
    ).fetchall()]

    with st.expander("🔍 Filters", expanded=False):
        f1, f2, f3 = st.columns(3)
        with f1:
            f_site = st.selectbox("Site", options=["(all)"] + all_sites, index=0, key="dash_f_site")
            f_title = st.text_input("Title contains", value="", key="dash_f_title",
                                     placeholder="e.g. prompt engineer")
        with f2:
            f_disc_from = st.date_input("Discovered from", value=None, key="dash_f_disc_from")
            f_disc_to = st.date_input("Discovered to", value=None, key="dash_f_disc_to")
        with f3:
            f_score_range = st.slider("Fit score range", min_value=0, max_value=10,
                                      value=(0, 10), step=1, key="dash_f_score")
            f_applied_only = st.checkbox("Only applied jobs", value=False, key="dash_f_applied_only")

        f4, f5, _ = st.columns([1, 1, 2])
        with f4:
            f_app_from = st.date_input("Applied from", value=None, key="dash_f_app_from")
        with f5:
            f_app_to = st.date_input("Applied to", value=None, key="dash_f_app_to")

        fc1, fc2, _ = st.columns([1, 1, 4])
        with fc1:
            if st.button("🔄 Reset filters", key="dash_f_reset"):
                for k in ("dash_f_site", "dash_f_title", "dash_f_disc_from", "dash_f_disc_to",
                          "dash_f_score", "dash_f_applied_only", "dash_f_app_from", "dash_f_app_to"):
                    if k in st.session_state:
                        del st.session_state[k]
                st.rerun()
        with fc2:
            f_limit = st.number_input("Max rows", min_value=10, max_value=2000, value=100,
                                      step=50, key="dash_f_limit")

    # Build the filter dict
    score_min, score_max = f_score_range
    df_str = f_disc_from.isoformat() if f_disc_from else None
    dt_str = f_disc_to.isoformat() if f_disc_to else None
    af_str = f_app_from.isoformat() if f_app_from else None
    at_str = f_app_to.isoformat() if f_app_to else None

    jobs = get_filtered_jobs(
        site=f_site,
        title_contains=f_title.strip() or None,
        discovered_from=df_str,
        discovered_to=dt_str,
        score_min=float(score_min) if score_min > 0 else None,
        score_max=float(score_max) if score_max < 10 else None,
        applied_from=af_str,
        applied_to=at_str,
        applied_only=f_applied_only,
        limit=int(f_limit),
    )

    # Summary line
    active_filters = []
    if f_site and f_site != "(all)":
        active_filters.append(f"site={f_site}")
    if f_title.strip():
        active_filters.append(f"title~'{f_title.strip()}'")
    if df_str or dt_str:
        active_filters.append(f"discovered {df_str or '…'} → {dt_str or '…'}")
    if score_min > 0 or score_max < 10:
        active_filters.append(f"score {score_min}-{score_max}")
    if af_str or at_str:
        active_filters.append(f"applied {af_str or '…'} → {at_str or '…'}")
    if f_applied_only:
        active_filters.append("applied only")
    filter_str = " · ".join(active_filters) if active_filters else "no filters"
    st.caption(f"Showing **{len(jobs)}** jobs · {filter_str}")

    if not jobs:
        st.info("No jobs match the current filters. Try widening the criteria or click 🔄 Reset filters.")
    else:
        # Render each job
        for job in jobs:
            url = job.get("url", "") or ""
            uk = hashlib.sha1(url.encode()).hexdigest()[:12]
            with st.container():
                c1, c2, c3, c4, c5 = st.columns([1, 4, 2, 2, 1])
                with c1:
                    st.write(score_badge(job.get("fit_score")))
                with c2:
                    title = job.get("title", "?")
                    st.markdown(f"**{title}**")
                    site = job.get("site", "") or "?"
                    location = job.get("location", "") or ""
                    st.caption(f"🌐 {site}" + (f" · 📍 {location}" if location else ""))
                with c3:
                    # Discovery + application dates
                    disc = fmt_date_full(job.get("discovered_at"))
                    st.caption(f"🔍 Discovered: **{disc}**")
                    app = job.get("applied_at")
                    if app:
                        st.caption(f"✅ Applied: **{fmt_date_full(app)}**")
                    else:
                        st.caption("✅ Applied: —")
                with c4:
                    # Tailoring / cover status
                    opts = []
                    if job.get("tailored_resume_path"):
                        opts.append("✅ Tailored")
                    else:
                        opts.append("⬜ Not tailored")
                    if job.get("cover_letter_path"):
                        opts.append("✅ Cover")
                    else:
                        opts.append("⬜ No cover")
                    st.caption(" · ".join(opts))
                with c5:
                    if st.button("View", key=f"dv_{uk}"):
                        st.query_params["job"] = url
                        st.query_params["page"] = "Job Detail"
                        st.rerun()
            # Per-row delete confirmation (own container for clean layout)
            with st.container():
                _, dc1, dc2 = st.columns([7, 1, 1])
                with dc1:
                    confirm_key = f"dc_c_{uk}"
                    confirmed = st.session_state.get(confirm_key, False)
                    if not confirmed:
                        if st.button("🗑️ Delete", key=f"dc_btn_{uk}"):
                            st.session_state[confirm_key] = True
                            st.rerun()
                    else:
                        st.warning("Confirm?")
                with dc2:
                    if st.session_state.get(confirm_key, False):
                        if st.button("Yes, delete", key=f"dc_yes_{uk}", type="primary"):
                            ok, msg = delete_job_by_url(url)
                            if ok:
                                st.success(msg)
                            else:
                                st.error(msg)
                            del st.session_state[confirm_key]
                            st.rerun()
            st.divider()

def page_jobs():
    conn = get_conn()
    st.title("💼 Job Bank")

    # Filters + Sort
    sites = ["All"] + [
        r[0] for r in conn.execute("SELECT DISTINCT site FROM jobs ORDER BY site").fetchall()
    ]
    
    # Initialize sort preference in session state
    if "job_sort_by" not in st.session_state:
        st.session_state["job_sort_by"] = "score"
    
    c1, c2, c3, c4 = st.columns([1, 1, 2, 1])
    min_score = c1.slider("Min Score", 1, 10, 6)
    site_sel = c2.selectbox("Source", sites)
    search = c3.text_input("🔍 Search", placeholder="Filter by title...")
    sort_by = c4.selectbox("Sort by", ["Score", "Date"], index=0 if st.session_state["job_sort_by"] == "score" else 1, key="job_sort_by")
    sort_key = "score" if sort_by == "Score" else "date"

    jobs = all_jobs(sort_by=sort_key)
    if site_sel != "All":
        jobs = [j for j in jobs if j.get("site") == site_sel]
    if search:
        jobs = [j for j in jobs if search.lower() in (j.get("title") or "").lower()]
    jobs = [j for j in jobs if (j.get("fit_score") or 0) >= min_score]

    st.caption(f"Showing {len(jobs)} of {len(all_jobs(sort_by=sort_key))} total jobs")

    for job in jobs:
        score = job.get("fit_score")
        url   = job.get("url", "")
        with st.container():
            c1, c2, c3, c4 = st.columns([1, 5, 2, 1])
            with c1:
                st.write(score_badge(score))
            with c2:
                st.markdown(f"**{job.get('title', '?')}**")
                st.caption(f"{job.get('site', '')} · {job.get('location', '')} · Found {fmt_discovered(job.get('discovered_at'))}")
            with c3:
                opts = []
                if job.get("tailored_resume_path"): opts.append("✅ Tailored")
                else: opts.append("⬜ Tailor")
                if job.get("cover_letter_path"):   opts.append("✅ Cover")
                else: opts.append("⬜ Cover")
                st.caption(" · ".join(opts))
            with c4:
                uk = hashlib.sha1(url.encode()).hexdigest()[:12]
                if st.button("View", key=f"jv_{uk}"):
                    st.query_params["job"] = url
                    st.query_params["page"] = "Job Detail"
                    st.rerun()
            st.divider()

def page_job_detail():
    st.title("📋 Job Detail")
    url = _current_job()
    if not url:
        st.info("No job selected. Go to **Jobs** and click **View** on any job.")
        return

    job = job_by_url(url)
    if not job:
        st.error("Job not found in database.")
        return

    # Update query_params so refresh stays on this job
    st.query_params["job"] = url

    st.header(job.get("title", "?"))
    c1, c2, c3, c4 = st.columns(4)
    c1.write(f"**Source:** {job.get('site', '?')}")
    c2.write(f"**Location:** {job.get('location', 'N/A')}")
    c3.write(f"**Score:** {score_badge(job.get('fit_score'))}")
    if job.get("salary"):
        c4.write(f"**Salary:** {job.get('salary')}")

    if job.get("application_url"):
        st.markdown(f"[📎 Apply URL]({job.get('application_url')})")

    tabs = st.tabs(["📝 Description", "🧠 Scoring", "📄 Resume", "💌 Cover Letter", "🚀 Apply"])

    # ── Description ───────────────────────────────────────────────────────────
    with tabs[0]:
        desc = job.get("full_description") or job.get("description") or ""
        st.text_area("Description", desc, height=400, label_visibility="collapsed")

    # ── Scoring ────────────────────────────────────────────────────────────────
    with tabs[1]:
        score      = job.get("fit_score")
        reasoning  = job.get("score_reasoning") or ""
        scored_at  = job.get("scored_at") or ""
        st.subheader(f"Fit Score: {score_badge(score)}")
        if reasoning:
            st.markdown("**Reasoning:**")
            st.info(reasoning)
        else:
            st.info("Not yet scored.")
        if scored_at:
            st.caption(f"Scored at: {scored_at}")

    # ── Resume ────────────────────────────────────────────────────────────────
    with tabs[2]:
        path  = job.get("tailored_resume_path") or ""
        text  = read_file_text(path)
        if text:
            st.text_area("Tailored Resume", text, height=400, label_visibility="collapsed")
        else:
            st.info("Not yet tailored. Run the **tailor** stage in Pipeline.")

    # ── Cover Letter ──────────────────────────────────────────────────────────
    with tabs[3]:
        path  = job.get("cover_letter_path") or ""
        text  = read_file_text(path)
        if text:
            st.text_area("Cover Letter", text, height=400, label_visibility="collapsed")
        else:
            st.info("Not yet generated. Run the **cover** stage in Pipeline.")

    # ── Apply ────────────────────────────────────────────────────────────────
    with tabs[4]:
        applied_at = job.get("applied_at")
        apply_url  = job.get("application_url") or job.get("url") or ""
        apply_st   = job.get("apply_status") or ""

        if applied_at:
            st.success(f"✅ Applied at {applied_at}")
            if apply_st:
                st.caption(f"Status: {apply_st}")

        elif apply_st == "pending_approval":
            st.warning("⏳ Awaiting your approval — you were notified on Telegram.")
            st.info("Open the app and go to the Dashboard to approve pending applications.")
            if st.button("❌ Decline"):
                from agents.auto_apply import mark_approval_declined
                conn2 = get_conn()
                mark_approval_declined(conn2, url)
                st.rerun()

        elif apply_st == "declined":
            st.caption("❌ You declined this application.")

        else:
            # Job has tailored resume + cover letter → offer approval queue
            has_resume = bool(job.get("tailored_resume_path"))
            has_cover  = bool(job.get("cover_letter_path"))

            st.markdown(f"**Apply URL:** [Open]({apply_url})")

            if has_resume and has_cover:
                st.success("✅ Resume tailored · ✅ Cover letter ready")

                # Queue for approval (idempotent — safe to call on every view)
                from agents.auto_apply import queue_for_approval
                try:
                    queue_for_approval(url)
                except Exception:
                    pass  # already queued or DB issue

                # Inline approval buttons — NO rerun until user commits
                col_a, col_b = st.columns(2)
                with col_a:
                    approved = st.button(
                        "✅ Yes — Open Apply in Browser",
                        type="primary",
                        key=f"approve_apply_{hashlib.sha1(url.encode()).hexdigest()[:8]}",
                    )
                with col_b:
                    declined = st.button(
                        "❌ No — Skip",
                        key=f"decline_apply_{hashlib.sha1(url.encode()).hexdigest()[:8]}",
                    )

                if approved:
                    import subprocess, os
                    def _load_env_for_apply():
                        env = os.environ.copy()
                        env["HOME"] = str(Path.home())
                        env_path = Path.home() / ".applypilot" / ".env"
                        if env_path.exists():
                            for line in env_path.read_text().splitlines():
                                line = line.strip()
                                if line and not line.startswith("#") and "=" in line:
                                    k, v = line.split("=", 1)
                                    env[k.strip()] = v.strip()
                        return env
                    result = subprocess.run(
                        ["python3", "-m", "applypilot", "apply",
                         "--url", url,
                         "--limit", "1",
                         "--headless"],
                        capture_output=True, text=True, timeout=300,
                        env=_load_env_for_apply(),
                    )
                    if result.returncode == 0:
                        st.success("✅ Application submitted!")
                    else:
                        err = result.stderr or result.stdout
                        st.error(f"Error: {err[:300] if err else 'Unknown error'}")
                    st.rerun()

                if declined:
                    from agents.auto_apply import mark_approval_declined
                    conn2 = get_conn()
                    mark_approval_declined(conn2, url)
                    st.rerun()

            elif has_resume:
                st.warning("✅ Resume tailored — run the **cover** stage first.")
                if st.button("✅ Mark as Applied (manual)"):
                    save_job_apply_status(url, "manual_submit_pending")
                    st.rerun()
            else:
                st.warning("Run **tailor** stage first to prepare your resume.")
                if st.button("✅ Mark as Applied (manual)"):
                    save_job_apply_status(url, "manual_submit_pending")
                    st.rerun()

def page_pipeline():
    st.title("⚙️ Pipeline")

    # ── Global pipeline controls ─────────────────────────────────────────────
    # These settings apply to ALL stage buttons in this section, so you can
    # tune the threshold once and run several stages with the same filter.
    st.subheader("🎛️ Pipeline controls")
    col_ctrl1, col_ctrl2, col_ctrl3 = st.columns(3)
    with col_ctrl1:
        # Default 0 (no filter) so this matches `applypilot run` default.
        # Users who want "only score >= 10" can set it to 10.
        min_score = st.slider(
            "Min fit score for tailor/cover",
            min_value=0, max_value=10, value=0, step=1,
            help=(
                "0 = no filter (process all scored jobs). "
                "7 = recommended (default for `applypilot run`). "
                "10 = only the very best matches. "
                "Score 1-3 is auto-skipped by the scorer anyway."
            ),
        )
    with col_ctrl2:
        since = st.selectbox(
            "Only process jobs discovered in…",
            options=["(no filter)", "Last 24h", "Last 7d", "Last 30d"],
            index=0,
            help=(
                "Useful for daily cron runs — only process new discoveries, "
                "don't re-tailor jobs from previous days."
            ),
        )
    with col_ctrl3:
        workers = st.number_input(
            "Parallel workers",
            min_value=1, max_value=8, value=1, step=1,
            help="Number of parallel threads for discover/enrich stages.",
        )

    # Translate the human-friendly labels into CLI flag values
    since_map = {"(no filter)": None, "Last 24h": "24h", "Last 7d": "7d", "Last 30d": "30d"}
    since_arg = since_map[since]
    # Stash so individual buttons can read the same values without re-rendering
    st.session_state["pipeline_min_score"] = min_score
    st.session_state["pipeline_since"] = since_arg
    st.session_state["pipeline_workers"] = workers

    # Show what the current selection will do
    if min_score > 0:
        st.info(
            f"📊 Currently filtering to **score ≥ {min_score}**. "
            f"{_count_jobs_above_score(min_score)} jobs in the DB match.",
            icon="🎯",
        )

    st.divider()

    stages = [
        ("discover",  "Discover Jobs",       "Search job boards (LinkedIn, Indeed, Glassdoor, ZipRecruiter)"),
        ("employers","Discover Employers",   "Scrape career pages from 18 target companies"),
        ("enrich",    "Enrich Details",       "Fetch full job descriptions"),
        ("score",     "Score Jobs",           "Rate jobs 1-10 using resume + Ollama"),
        ("tailor",    "Tailor Resume",        f"Rewrite resume bullets for score ≥ {min_score if min_score else 7} jobs"),
        ("cover",     "Write Cover Letter",   "Generate personalised cover letters (English)"),
        ("cover_sl",  "Write Cover Letter (SL)", "Generate Slovenian cover letters for mojedelo jobs"),
        ("pdf",       "Export PDF",           "Convert to PDF"),
    ]

    last_rc  = [None]
    last_out = [None]

    for stage, label, desc in stages:
        with st.expander(f"▶ {label} — `{stage}`", expanded=(stage in ["tailor","cover"])):
            st.caption(desc)
            col_btn, col_rc = st.columns([4, 1])
            with col_btn:
                if st.button(f"▶ Run {label}", key=f"run_{stage}"):
                    with st.spinner(f"Running `{stage}`..."):
                        output, rc = run_stage(
                            stage,
                            min_score=min_score,
                            since=since_arg,
                            workers=workers,
                        )
                    last_out[0] = output[-4000:]
                    last_rc[0]  = rc
                    st.rerun()
            with col_rc:
                st.write(f"Exit: {last_rc[0] if last_rc[0] is not None else '—'}")

        if last_out[0] is not None and last_rc[0] is not None:
            with st.expander("Output", expanded=False):
                st.code(last_out[0], language="bash")
                if last_rc[0] == 0:
                    st.success(f"`{stage}` completed successfully.")
                else:
                    st.error(f"`{stage}` failed (exit {last_rc[0]}).")
            last_out[0], last_rc[0] = None, None

    st.divider()

    # ── Custom URL pipeline ──────────────────────────────────────────────────
    st.subheader("🔗 Custom URL Pipeline")
    st.caption("Paste a job URL and run the full pipeline (insert → enrich → score → tailor → cover) on it.")

    with st.form("custom_url_form", clear_on_submit=False):
        url = st.text_input(
            "Job URL",
            placeholder="https://app.welcometothejungle.com/jobs/xxxxx",
            help="Any job URL — WTTJ, LinkedIn, Indeed, mojedelo, or any career page.",
        )
        col1, col2, col3 = st.columns(3)
        with col1:
            run_enrich  = st.checkbox("Enrich (fetch description)", value=True)
        with col2:
            run_score   = st.checkbox("Score (1-10 fit)",           value=True)
        with col3:
            run_tailor  = st.checkbox("Tailor + Cover (if score ≥ 7)", value=True)

        submitted = st.form_submit_button("🚀 Run pipeline on URL", type="primary")

    if submitted:
        if not url or not url.strip().startswith(("http://", "https://")):
            st.error("Please enter a valid URL starting with http:// or https://")
        else:
            _run_custom_url_pipeline(url.strip(), run_enrich, run_score, run_tailor)


def _run_custom_url_pipeline(url: str, do_enrich: bool, do_score: bool, do_tailor: bool):
    """Insert a custom URL into the DB and run downstream pipeline stages."""
    import sqlite3
    from datetime import datetime, timezone

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()

    # Detect site label from URL hostname
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    site_map = {
        "welcometothejungle.com": "welcometothejungle",
        "mojedelo.com":           "mojedelo",
        "weworkremotely.com":     "weworkremotely",
        "linkedin.com":           "linkedin",
        "indeed.com":             "indeed",
        "glassdoor.com":          "glassdoor",
        "google.com":             "google",
    }
    site_label = next((v for k, v in site_map.items() if k in host), host or "manual")

    # Try to extract a job title from the URL slug
    title_guess = ""
    try:
        from urllib.parse import unquote
        path = urlparse(url).path
        # last non-empty path segment
        parts = [p for p in path.split("/") if p and p not in ("jobs", "job", "delovno-mesto", "positions")]
        if parts:
            title_guess = unquote(parts[-1]).replace("-", " ").replace("_", " ")[:120].strip()
    except Exception:
        pass

    # Insert (or update) the job
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO jobs (url, title, site, strategy, discovered_at)
            VALUES (?, ?, ?, 'custom_url_form', ?)
        """, (url, title_guess or "Custom URL", site_label, now))
        conn.commit()
        st.success(f"✅ Inserted new job: {title_guess or url}")
    except sqlite3.IntegrityError:
        # Already exists — reset downstream fields so we re-process
        cur.execute("""
            UPDATE jobs
            SET fit_score=NULL, score_reasoning=NULL, scored_at=NULL,
                tailored_resume_path=NULL, tailored_at=NULL, tailor_attempts=0,
                cover_letter_path=NULL, cover_letter_at=NULL,
                full_description=NULL, detail_scraped_at=NULL, detail_error=NULL
            WHERE url = ?
        """, (url,))
        conn.commit()
        st.info(f"ℹ️ Job already exists — reset pipeline fields. Re-running…")

    conn.close()

    # Run downstream stages
    stages_run = []
    if do_enrich:
        with st.spinner("Enriching (fetching full description)…"):
            out, rc = run_stage("enrich", timeout=900)
        stages_run.append(("enrich", rc, out[-2000:]))
    if do_score:
        with st.spinner("Scoring (LLM fit 1-10)…"):
            out, rc = run_stage("score", timeout=600)
        stages_run.append(("score", rc, out[-2000:]))
    if do_tailor:
        with st.spinner("Tailoring resume + writing cover letter…"):
            out, rc = run_stage("tailor", timeout=600)
            out2, rc2 = run_stage("cover",  timeout=600)
        stages_run.append(("tailor", rc, out[-2000:]))
        stages_run.append(("cover",  rc2, out2[-2000:]))

    # Summary
    all_ok = all(rc == 0 for _, rc, _ in stages_run)
    for stage, rc, output in stages_run:
        if rc == 0:
            st.success(f"✅ `{stage}` OK")
        else:
            st.error(f"❌ `{stage}` failed (exit {rc})")
        with st.expander(f"{stage} output", expanded=False):
            st.code(output, language="bash")

    if all_ok:
        # Fetch the resulting job from DB to show what was made
        c2 = sqlite3.connect(str(DB_PATH), timeout=30)
        c2.row_factory = sqlite3.Row
        row = c2.execute("SELECT title, fit_score, tailored_resume_path, cover_letter_path FROM jobs WHERE url=?", (url,)).fetchone()
        c2.close()
        if row:
            st.subheader("Result")
            st.write(f"**Title:** {row['title']}")
            st.write(f"**Fit score:** {row['fit_score']}/10")
            if row['tailored_resume_path']:
                st.write(f"**Tailored resume:** `{row['tailored_resume_path']}`")
            if row['cover_letter_path']:
                st.write(f"**Cover letter:** `{row['cover_letter_path']}`")
        st.balloons()
    else:
        st.warning("Some stages failed — check output above.")

def page_settings():
    st.title("⚙️ Settings")

    # Profile editor
    st.subheader("👤 Profile (profile.json)")
    profile_text = ""
    if PROFILE_PATH.exists():
        profile_text = PROFILE_PATH.read_text()
    updated = st.text_area("profile.json", profile_text, height=300, label_visibility="collapsed")
    if st.button("💾 Save profile.json"):
        PROFILE_PATH.write_text(updated)
        st.success("Saved profile.json")

    st.divider()

    # Searches editor
    st.subheader("🔍 Job Searches (searches.yaml)")
    searches_text = ""
    if SEARCHES_PATH.exists():
        searches_text = SEARCHES_PATH.read_text()
    updated_s = st.text_area("searches.yaml", searches_text, height=300, label_visibility="collapsed")
    if st.button("💾 Save searches.yaml"):
        SEARCHES_PATH.write_text(updated_s)
        st.success("Saved searches.yaml")

    st.divider()

    # Resume viewer
    st.subheader("📄 Resume (resume.txt)")
    if RESUME_PATH.exists():
        st.code(RESUME_PATH.read_text()[:3000], language="unicode")
    else:
        st.info("resume.txt not found. Run `pdftotext` on your CV PDF to create it.")

    st.divider()

    # Auto-search controls
    st.subheader("🔄 Auto-Search Setup")
    ast = load_autosearch()

    cron_jobs = cronjob_safe(action='list').get("jobs", [])
    has_cron = len(cron_jobs) > 0
    auto_job = next((j for j in cron_jobs if j.get("name") == "Auto Job Discovery"), None)

    st.write("**Current status:**", "✅ Enabled" if ast.get("enabled") else "⏹️ Disabled")
    st.write("**Interval:**", f"{ast.get('interval_min', 30)} minutes")

    with st.expander("Manual Cron Setup (Required if running outside Hermes)"):
        st.markdown("""
To enable automatic job discovery, add this to your crontab:

```bash
# Edit crontab
EDITOR=nano crontab -e

# Add line (runs every X minutes, e.g., 30):
*/30 * * * * export HOME=/home/bostjan && cd /media/bostjan/Documents/Osebno/ZAPOSLITEV/AI\ JOB\ 2026 && python3 -m applypilot run discover >> ~/.applypilot/autosearch.log 2>&1

# For 60 minutes:
0 * * * * export HOME=/home/bostjan && cd /media/bostjan/Documents/Osebno/ZAPOSLITEV/AI\ JOB\ 2026 && python3 -m applypilot run discover >> ~/.applypilot/autosearch.log 2>&1
```
        """)
        st.info("The UI toggle above saves your settings so the Dashboard shows the correct status.")

    st.divider()

    # LinkedIn profile scraper
    st.subheader("🔗 LinkedIn Profile Review")
    st.caption("Uses Chrome CDP to scrape your LinkedIn profile (requires Chrome running with --remote-debugging-port=9222 and valid li_at cookie)")

    col_li_btn, col_li_data = st.columns([1, 3])
    with col_li_btn:
        if st.button("📋 Fetch My LinkedIn Profile", key="fetch_linkedin"):
            with st.spinner("Connecting to Chrome CDP..."):
                data = _fetch_linkedin_profile()
            st.session_state["linkedin_data"] = data
            st.rerun()

    if "linkedin_data" in st.session_state:
        data = st.session_state["linkedin_data"]
        if "error" in data:
            st.warning(data["error"])
        else:
            st.success(f"✅ Fetched profile — {data.get('raw_length', 0)} chars HTML")
            if data.get("name"):
                st.info(f"👤 {data['name']}")
            if data.get("headline"):
                st.info(f"💼 {data['headline']}")
            if data.get("about"):
                st.text_area("About", data["about"], height=150, label_visibility="collapsed")
            st.caption("Note: LinkedIn heavily dynamically loads profile content. For best results, open your profile in the CDP Chrome tab first, then run fetch.")

    st.divider()

    # Telegram config
    st.subheader("📲 Telegram Notifications")
    st.caption("Set these env vars or add them to ~/.applypilot/.env")
    st.code("""# In ~/.applypilot/.env
TELEGRAM_BOT_TOKEN=***
TELEGRAM_CHAT_ID=your_chat_id

# To get a bot token: message @BotFather on Telegram
# To get your chat_id: message @userinfobot on Telegram
""")
    st.info("Telegram alerts are sent automatically when a job is queued for approval and when an application is submitted.")

    st.divider()

    # ── Database management ──────────────────────────────────────────────────
    st.subheader("🗄️ Database Management")
    st.caption("Clean up old jobs. Operations are permanent — use with care.")

    # Show current DB stats
    try:
        import sqlite3 as _sql
        c = _sql.connect(str(DB_PATH), timeout=10)
        total      = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        scored     = c.execute("SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL").fetchone()[0]
        tailored   = c.execute("SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL").fetchone()[0]
        applied    = c.execute("SELECT COUNT(*) FROM jobs WHERE apply_status = 'applied'").fetchone()[0]
        c.close()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total",     total)
        c2.metric("Scored",    scored)
        c3.metric("Tailored",  tailored)
        c4.metric("Applied",   applied)
    except Exception as e:
        st.error(f"DB read error: {e}")

    st.write("")

    # Delete all
    st.markdown("##### 🗑️ Delete all jobs")
    with st.form("delete_all_form", clear_on_submit=False):
        st.warning("⚠️ This will delete **every** job in the database. Cannot be undone.")
        confirm_all = st.text_input('Type **DELETE ALL** to confirm', key="confirm_all")
        delete_all_btn = st.form_submit_button("🗑️ Delete all jobs", type="secondary")
    if delete_all_btn:
        if confirm_all.strip() != "DELETE ALL":
            st.error("Confirmation text doesn't match. No rows deleted.")
        else:
            try:
                c = sqlite3.connect(str(DB_PATH), timeout=30)
                cur = c.cursor()
                cur.execute("SELECT COUNT(*) FROM jobs")
                before = cur.fetchone()[0]
                cur.execute("DELETE FROM jobs")
                deleted = cur.rowcount
                c.commit()
                c.close()
                st.success(f"✅ Deleted {deleted} of {before} jobs.")
                st.rerun()
            except Exception as e:
                st.error(f"Delete failed: {e}")

    st.write("")

    # Delete older than
    st.markdown("##### 📅 Delete jobs older than a date")
    with st.form("delete_older_form", clear_on_submit=False):
        cutoff_date = st.date_input("Delete jobs discovered before this date", value=None)
        # Also offer status filter
        only_unscored = st.checkbox("Only delete jobs that have NOT been scored", value=False)
        col_a, col_b = st.columns(2)
        with col_a:
            dry_run = st.form_submit_button("🔍 Preview (count only)")
        with col_b:
            really_delete = st.form_submit_button("🗑️ Delete", type="secondary")

    if cutoff_date and (dry_run or really_delete):
        try:
            cutoff_iso = f"{cutoff_date.isoformat()}T00:00:00"
            c = sqlite3.connect(str(DB_PATH), timeout=30)
            cur = c.cursor()
            if only_unscored:
                count_q = "SELECT COUNT(*) FROM jobs WHERE discovered_at < ? AND fit_score IS NULL"
                del_q   = "DELETE FROM jobs WHERE discovered_at < ? AND fit_score IS NULL"
            else:
                count_q = "SELECT COUNT(*) FROM jobs WHERE discovered_at < ?"
                del_q   = "DELETE FROM jobs WHERE discovered_at < ?"
            n = cur.execute(count_q, (cutoff_iso,)).fetchone()[0]
            if dry_run:
                st.info(f"Would delete **{n}** jobs discovered before {cutoff_date}"
                        + (" (unscored only)" if only_unscored else ""))
            else:
                if n == 0:
                    st.info("No jobs matched — nothing deleted.")
                else:
                    # Require a second confirmation
                    confirm = st.text_input(
                        f'Type **{n}** to confirm deletion of {n} jobs',
                        key=f"confirm_older_{n}"
                    )
                    if confirm.strip() == str(n):
                        cur.execute(del_q, (cutoff_iso,))
                        c.commit()
                        st.success(f"✅ Deleted {cur.rowcount} jobs.")
                        st.rerun()
                    else:
                        st.warning(f"Type the number {n} to confirm.")
            c.close()
        except Exception as e:
            st.error(f"Operation failed: {e}")

    st.write("")

    # Reset failed-applies
    st.markdown("##### 🔄 Reset failed applications")
    st.caption("Clear `apply_status='failed'` so they can be retried.")
    if st.button("🔄 Reset all failed jobs", key="reset_failed"):
        try:
            c = sqlite3.connect(str(DB_PATH), timeout=30)
            cur = c.cursor()
            cur.execute("""
                UPDATE jobs SET apply_status=NULL, apply_error=NULL,
                               apply_attempts=0
                WHERE apply_status='failed'
            """)
            n = cur.rowcount
            c.commit()
            c.close()
            st.success(f"✅ Reset {n} failed jobs.")
            st.rerun()
        except Exception as e:
            st.error(f"Reset failed: {e}")


def page_site_analytics():
    """Per-site success-rate analytics (4.9) + dynamic blacklist (4.10) + form schema cache (4.8) + Telegram status (4.6)."""
    st.title("📊 Site Analytics & Safety Rails")
    st.caption("Per-site apply success rate, dynamic blacklist, form schema cache, and Telegram notifier status.")

    try:
        from applypilot.database import (
            get_site_stats, get_dynamic_blacklist,
            is_site_blacklisted, get_blacklist_as_dict,
        )
        from applypilot.apply.form_schema_cache import get_cache_stats, get_schema, get_schema_for_prompt
        from applypilot.apply.notifier import _load_creds, reset_cache as reset_notifier_cache
    except ImportError as e:
        st.error(f"Could not import applypilot modules: {e}\n\nMake sure you're running from the applypilot Python environment.")
        return

    # ----- Controls -----
    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        days = st.number_input("Look-back window (days)", min_value=1, max_value=365, value=30, step=1)
    with col2:
        min_attempts = st.number_input("Min attempts per site", min_value=1, max_value=100, value=3, step=1)
    with col3:
        env_blacklist_on = os.environ.get("APPLY_ENABLE_BLACKLIST", "").strip() in ("1", "true", "yes")
        st.metric("Blacklist env", "ON ✅" if env_blacklist_on else "OFF ⏸️",
                  delta="APPLY_ENABLE_BLACKLIST=1" if not env_blacklist_on else "active",
                  delta_color="off")

    st.divider()

    # ----- Section 1: Telegram status (4.6) -----
    st.subheader("📨 Telegram Notifier (4.6)")
    token, chat_id = _load_creds()
    col_t1, col_t2 = st.columns([3, 1])
    with col_t1:
        if token and chat_id:
            masked_t = token[:8] + "..." + token[-4:] if len(token) > 12 else "(short)"
            st.success(f"✅ Telegram configured — bot token `{masked_t}`, chat_id `{chat_id}`")
            # Test send
            test_msg = st.text_input("Test message", "✅ ApplyPilot dashboard test")
            if st.button("📨 Send test message", key="tg_test"):
                from applypilot.apply.notifier import _send_telegram_message
                ok = _send_telegram_message(token, chat_id, test_msg)
                if ok:
                    st.success("Sent! Check your Telegram.")
                else:
                    st.error("Send failed — check token/chat_id and network.")
        else:
            missing = []
            if not token:
                missing.append("TELEGRAM_BOT_TOKEN (or APPLY_TELEGRAM_BOT_TOKEN)")
            if not chat_id:
                missing.append("TELEGRAM_CHAT_ID (or APPLY_TELEGRAM_CHAT_ID)")
            st.error(f"❌ Telegram not configured. Missing: {', '.join(missing)}")
            st.markdown("""
**How to configure:**
1. Open Telegram, search for `@BotFather`, send `/newbot`
2. Get your bot token (e.g. `1234567890:AAF...`)
3. Send any message to your new bot, then visit:
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and find `"chat":{"id":...}`
4. Add to `~/.applypilot/.env`:
   ```
   TELEGRAM_BOT_TOKEN=<paste token>
   TELEGRAM_CHAT_ID=<numeric id>
   ```
""")
    with col_t2:
        if st.button("🔄 Reload creds", key="tg_reload"):
            reset_notifier_cache()
            st.rerun()

    st.divider()

    # ----- Section 2: Per-site stats table (4.9) -----
    st.subheader(f"🌐 Per-Site Success Rate (last {days} days, min {min_attempts} attempts) — 4.9")
    try:
        sites_data = get_site_stats(days=days, min_attempts=min_attempts)
    except Exception as e:
        st.error(f"Failed to load site stats: {e}")
        sites_data = []

    if not sites_data:
        st.info(f"No sites have at least {min_attempts} apply attempts in the last {days} days. Lower the min or run more applies.")
    else:
        # Build a pandas dataframe for nice display
        import pandas as pd
        rows = []
        for s in sites_data:
            failed_total = s["failed"] + s["expired"] + s["captcha"] + s["login_issue"]
            rows.append({
                "Site": s["site"],
                "Attempts": s["attempts"],
                "Applied": s["applied"],
                "Failed": failed_total,
                "Success %": f"{s['success_rate']*100:.0f}%",
                "Streak": s["recent_failure_streak"],
                "Status": "🚫 BLACKLISTED" if (s["failure_rate"] > 0.85 and s["recent_failure_streak"] >= 3) else "✅ OK",
            })
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

    st.divider()

    # ----- Section 3: Dynamic blacklist (4.10) -----
    st.subheader("🚫 Dynamic Blacklist — 4.10")
    try:
        blacklist = get_dynamic_blacklist(days=days, min_attempts=min_attempts)
    except Exception as e:
        st.error(f"Failed to compute blacklist: {e}")
        blacklist = []

    col_b1, col_b2 = st.columns([3, 1])
    with col_b1:
        if blacklist:
            st.warning(f"**{len(blacklist)} site(s) currently blacklisted** (failure rate > 85% AND streak ≥ 3):")
            for entry in blacklist:
                with st.expander(f"🚫 {entry['site']} — {entry['failure_rate']*100:.0f}% fail rate, {entry['recent_failure_streak']}-streak, {entry['attempts']} attempts"):
                    st.markdown(f"""
- **Site:** `{entry['site']}`
- **Attempts:** {entry['attempts']}
- **Applied:** {entry['applied']}
- **Failed:** {entry['failed']} | **Expired:** {entry['expired']} | **Captcha:** {entry['captcha']} | **Login issue:** {entry['login_issue']}
- **Success rate:** {entry['success_rate']*100:.1f}%
- **Failure rate:** {entry['failure_rate']*100:.1f}%
- **Recent failure streak:** {entry['recent_failure_streak']}
""")
        else:
            st.success("✅ No sites currently blacklisted.")
    with col_b2:
        st.markdown("**Tune thresholds (env vars):**")
        st.code("""APPLY_BLACKLIST_FAILURE_THRESHOLD=0.85
APPLY_BLACKLIST_STREAK_THRESHOLD=3
APPLY_BLACKLIST_DAYS=30
APPLY_BLACKLIST_MIN_ATTEMPTS=3
APPLY_ENABLE_BLACKLIST=1   # turn on""", language="bash")

    st.divider()

    # ----- Section 4: Form schema cache (4.8) -----
    st.subheader("🗂️ Form Schema Cache — 4.8")
    try:
        cache_stats = get_cache_stats()
    except Exception as e:
        st.error(f"Failed to load cache stats: {e}")
        cache_stats = {"total_sites": 0, "total_uses": 0, "total_successes": 0,
                       "total_failures": 0, "overall_success_rate": 0.0, "sites": []}

    col_c1, col_c2, col_c3, col_c4 = st.columns(4)
    col_c1.metric("Sites cached", cache_stats["total_sites"])
    col_c2.metric("Total uses", cache_stats["total_uses"])
    col_c3.metric("Cache successes", cache_stats["total_successes"])
    col_c4.metric("Cache success rate", f"{cache_stats['overall_success_rate']*100:.0f}%")

    if cache_stats["sites"]:
        st.markdown("**Cached sites:**")
        cols = st.columns(min(4, len(cache_stats["sites"])))
        for i, site in enumerate(cache_stats["sites"]):
            with cols[i % len(cols)]:
                schema = get_schema(site)
                if schema:
                    st.markdown(f"**{site}** — uses={schema.get('uses', 0)}, "
                                f"✅{schema.get('successes', 0)} / ❌{schema.get('failures', 0)}")
                    with st.expander(f"View schema for {site}"):
                        st.code(get_schema_for_prompt(site), language="markdown")
    else:
        st.info("No form schemas cached yet. They populate automatically as the apply agent learns each site's form structure.")

    # Prune button
    if st.button("🧹 Prune stale entries (>30d, no successes)", key="fsc_prune"):
        from applypilot.apply.form_schema_cache import prune_stale
        n = prune_stale(threshold_days=30, min_attempts=3)
        st.success(f"Pruned {n} stale entries.")
        st.rerun()


if selection == "Dashboard":
    page_dashboard()
elif selection == "Jobs":
    page_jobs()
elif selection == "Job Detail":
    page_job_detail()
elif selection == "Pipeline":
    page_pipeline()
elif selection == "Site Analytics":
    page_site_analytics()
elif selection == "Settings":
    page_settings()
