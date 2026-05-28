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

# ── Paths ────────────────────────────────────────────────────────────────────
APPLYPILOT_DIR = Path.home() / ".applypilot"
DB_PATH        = APPLYPILOT_DIR / "applypilot.db"
RESUME_PATH    = APPLYPILOT_DIR / "resume.txt"
TAILORED_DIR   = APPLYPILOT_DIR / "tailored_resumes"
COVER_DIR      = APPLYPILOT_DIR / "cover_letters"
PROFILE_PATH   = APPLYPILOT_DIR / "profile.json"
SEARCHES_PATH  = APPLYPILOT_DIR / "searches.yaml"

# ── Add project agents/ to path for auto_apply module ─────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# ── DB cursor helper ──────────────────────────────────────────────────────────
def get_conn():
    return sqlite3.connect(str(DB_PATH), timeout=30)

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

def all_jobs() -> list[dict]:
    conn = get_conn()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY fit_score DESC NULLS LAST, discovered_at DESC LIMIT 500"
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

def run_stage(stage: str, timeout=600) -> tuple[str, int]:
    """Run applypilot stage. Returns (output, returncode)."""
    env = os.environ.copy()
    env["HOME"] = str(Path.home())
    res = subprocess.run(
        ["python3", "-m", "applypilot", "run", stage],
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

# ── Page config ──────────────────────────────────────────────────────────────
PAGES = ["Dashboard", "Jobs", "Job Detail", "Pipeline", "Settings"]

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
def page_dashboard():
    conn = get_conn()
    total    = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    scored   = conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL").fetchone()[0]
    tailored = conn.execute("SELECT COUNT(*) FROM jobs WHERE tailored_at IS NOT NULL").fetchone()[0]
    cover    = conn.execute("SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL").fetchone()[0]
    applied  = conn.execute("SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL").fetchone()[0]
    ready    = conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score >= 7").fetchone()[0]

    st.title("📊 Pipeline Dashboard")
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
    pending = conn.execute("""
        SELECT * FROM jobs WHERE apply_status = 'pending_approval' ORDER BY fit_score DESC LIMIT 5
    """).fetchall()
    if pending:
        pending_dicts = [row_to_dict(r) for r in pending]
        for job in pending_dicts:
            with st.container():
                c1, c2, c3 = st.columns([1, 5, 1])
                c1.write(score_badge(job.get("fit_score")))
                c2.markdown(f"**{job.get('title', '?')}**\n{c2.caption(f\"{job.get('site', '')} · {job.get('location', '')}\")}")
                uk = hashlib.sha1((job.get("url") or "").encode()).hexdigest()[:8]
                if c3.button("Approve", key=f"pend_approve_{uk}"):
                    st.session_state["_pending_approve"] = job.get("url")
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
    st.subheader("Recent Jobs")
    recent = all_jobs()[:10]
    for job in recent:
        with st.container():
            c1, c2, c3, c4 = st.columns([1, 4, 2, 1])
            with c1:
                st.write(score_badge(job.get("fit_score")))
            with c2:
                st.markdown(f"**{job.get('title', '?')}**")
                st.caption(f"{job.get('site', '')} · {job.get('location', '')}")
            with c3:
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
            with c4:
                url = job.get("url", "")
                uk = hashlib.sha1(url.encode()).hexdigest()[:12]
                if st.button("View", key=f"dv_{uk}"):
                    st.query_params["job"] = url
                    st.query_params["page"] = "Job Detail"
                    st.rerun()
            st.divider()

def page_jobs():
    conn = get_conn()
    st.title("💼 Job Bank")

    # Filters
    sites = ["All"] + [
        r[0] for r in conn.execute("SELECT DISTINCT site FROM jobs ORDER BY site").fetchall()
    ]
    c1, c2, c3 = st.columns([1, 1, 2])
    min_score = c1.slider("Min Score", 1, 10, 6)
    site_sel = c2.selectbox("Source", sites)
    search  = c3.text_input("🔍 Search", placeholder="Filter by title...")

    jobs = all_jobs()
    if site_sel != "All":
        jobs = [j for j in jobs if j.get("site") == site_sel]
    if search:
        jobs = [j for j in jobs if search.lower() in (j.get("title") or "").lower()]
    jobs = [j for j in jobs if (j.get("fit_score") or 0) >= min_score]

    st.caption(f"Showing {len(jobs)} of {len(all_jobs())} total jobs")

    for job in jobs:
        score = job.get("fit_score")
        url   = job.get("url", "")
        with st.container():
            c1, c2, c3, c4 = st.columns([1, 5, 2, 1])
            with c1:
                st.write(score_badge(score))
            with c2:
                st.markdown(f"**{job.get('title', '?')}**")
                st.caption(f"{job.get('site', '')} · {job.get('location', '')}")
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
                    from agents.auto_apply import submit_application
                    result = submit_application(url, approved=True)
                    if result.get("status") == "submitted":
                        st.success("✅ Application submitted! Check your browser.")
                    else:
                        st.error(f"Error: {result.get('message', 'Unknown error')}")
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

    stages = [
        ("discover",  "Discover Jobs",       "Search job boards (LinkedIn, Indeed, Glassdoor, ZipRecruiter)"),
        ("employers","Discover Employers",   "Scrape career pages from 18 target companies"),
        ("enrich",    "Enrich Details",       "Fetch full job descriptions"),
        ("score",     "Score Jobs",           "Rate jobs 1-10 using resume + Ollama"),
        ("tailor",    "Tailor Resume",        "Rewrite resume bullets for score ≥ 7 jobs"),
        ("cover",     "Write Cover Letter",   "Generate personalised cover letters"),
        ("pdf",       "Export PDF",           "Convert to PDF"),
    ]

    for stage, label, desc in stages:
        with st.expander(f"▶ {label} — `{stage}`", expanded=(stage in ["tailor","cover"])):
            st.caption(desc)
            out, rc = st.columns([4, 1])
            with out:
                if st.button(f"▶ Run {label}", key=f"run_{stage}"):
                    with st.spinner(f"Running `{stage}`..."):
                        output, rc = run_stage(stage)
                    st.code(output[-4000:] if len(output) > 4000 else output, language="bash")
                    if rc == 0:
                        st.success(f"`{stage}` completed successfully.")
                    else:
                        st.error(f"`{stage}` failed (exit {rc}).")
            with rc:
                st.write(f"Exit: {rc if 'rc' in dir() else '—'}")

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

    # Telegram config
    st.subheader("📲 Telegram Notifications")
    st.caption("Set these env vars or add them to ~/.applypilot/.env")
    st.code("""# In ~/.applypilot/.env
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# To get a bot token: message @BotFather on Telegram
# To get your chat_id: message @userinfobot on Telegram
""")
    st.info("Telegram alerts are sent automatically when a job is queued for approval and when an application is submitted.")

# ── Route ────────────────────────────────────────────────────────────────────
if selection == "Dashboard":
    page_dashboard()
elif selection == "Jobs":
    page_jobs()
elif selection == "Job Detail":
    page_job_detail()
elif selection == "Pipeline":
    page_pipeline()
elif selection == "Settings":
    page_settings()
