"""ApplyPilot Web UI — Streamlit frontend for the job application pipeline."""

import io
import os
import sys
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

import streamlit as st

# ── Bootstrap ────────────────────────────────────────────────────────────────

APPLYPILOT_PKG = "/home/bostjan/.local/lib/python3.12/site-packages"
if APPLYPILOT_PKG not in sys.path:
    sys.path.insert(0, APPLYPILOT_PKG)

# Make applypilot config importable
os.environ["HOME"] = "/home/bostjan"
os.environ["LLM_URL"] = "http://127.0.0.1:11434/v1"

from applypilot.config import APP_DIR, DB_PATH, RESUME_PDF_PATH, SEARCH_CONFIG_PATH, load_env
from applypilot.database import get_stats, get_jobs_by_stage, get_connection

# Ensure HOME for Streamlit
os.environ["HOME"] = "/home/bostjan"
load_env()

st.set_page_config(
    page_title="ApplyPilot",
    page_icon="🎯",
    layout="wide",
    menu_items={
        "About": "AI-powered job application automation — powered by Ollama + ApplyPilot"
    }
)

# ── Constants ────────────────────────────────────────────────────────────────

DB = Path("/home/bostjan/.applypilot/applypilot.db")
APP_DIR = Path("/home/bostjan/.applypilot")
COVER_DIR = APP_DIR / "cover_letters"
TAILORED_DIR = APP_DIR / "tailored_resumes"
RESUME_TXT = APP_DIR / "resume.txt"

MIN_SCORE_DEFAULT = 7

# ── Helpers ──────────────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def get_stats_cached():
    return get_stats()

@st.cache_data(ttl=30)
def get_jobs_cached(stage="scored", min_score=7, limit=200):
    return get_jobs_by_stage(stage=stage, min_score=min_score, limit=limit)

def get_all_jobs(limit=500):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY fit_score DESC NULLS LAST, discovered_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    if not rows:
        return []
    cols = rows[0].keys()
    return [dict(zip(cols, r)) for r in rows]

def read_file(path: Path) -> str:
    if path and path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    return ""

def score_badge(score: int | None) -> str:
    if score is None:
        return "⚪ Unscored"
    if score >= 8:
        return f"🟢 {score}"
    if score >= 7:
        return f"🟡 {score}"
    if score >= 5:
        return f"🟠 {score}"
    return f"🔴 {score}"

def run_pipeline_stage(stage: str) -> str:
    """Run a pipeline stage and return output."""
    env = os.environ.copy()
    env["HOME"] = "/home/bostjan"
    result = subprocess.run(
        ["python3", "-m", "applypilot", "run", stage],
        capture_output=True,
        text=True,
        timeout=600,
        cwd="/home/bostjan",
        env=env,
    )
    return result.stdout + result.stderr

def run_applypilot_command(cmd: list[str], timeout=600) -> tuple[str, str, int]:
    """Run an applypilot CLI command. Returns (stdout, stderr, returncode)."""
    env = os.environ.copy()
    env["HOME"] = "/home/bostjan"
    result = subprocess.run(
        ["python3", "-m", "applypilot"] + cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd="/home/bostjan",
        env=env,
    )
    return result.stdout, result.stderr, result.returncode

# ── Page: Dashboard ─────────────────────────────────────────────────────────

def page_dashboard():
    st.title("🎯 ApplyPilot — Job Application Pipeline")

    stats = get_stats_cached()

    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("Total Jobs", stats["total"])
    col2.metric("Discovered", stats["total"])
    col3.metric("With Descriptions", stats["with_description"])
    col4.metric("Scored", stats["scored"])
    col5.metric("Tailored", stats["tailored"])
    col6.metric("Cover Letters", stats["with_cover_letter"])

    st.divider()

    # Score distribution
    col_left, col_right = st.columns([1, 1])

    with col_left:
        st.subheader("📊 Score Distribution")
        dist = stats.get("score_distribution", [])
        if dist:
            dist_data = sorted(dist, key=lambda x: x[0] or 0, reverse=True)
            chart_data = {"score": [str(r[0]) for r in dist_data], "count": [r[1] for r in dist_data]}
            st.bar_chart(chart_data, x="score", y="count")
        else:
            st.info("No scored jobs yet. Run the score stage.")

    with col_right:
        st.subheader("📋 Pipeline Summary")
        pipeline_items = [
            ("Discover", stats["total"], stats["pending_detail"], "discover"),
            ("Enrich", stats["with_description"], stats.get("pending_detail", 0), "enrich"),
            ("Score", stats["scored"], stats.get("unscored", 0), "score"),
            ("Tailor", stats["tailored"], stats.get("untailored_eligible", 0), "tailor"),
            ("Cover Letter", stats["with_cover_letter"], 0, "cover"),
            ("Applied", stats.get("applied", 0), stats.get("ready_to_apply", 0), "apply"),
        ]
        for name, done, pending, stage in pipeline_items:
            p = st.progress(done / max(stats["total"], 1), text=f"{name}: {done} done, {pending} pending")
            if st.button(f"▶ Run {name}", key=f"btn_{stage}"):
                with st.spinner(f"Running {name}..."):
                    output = run_pipeline_stage(stage)
                    st.code(output[-2000:] if len(output) > 2000 else output, language="bash")
                    st.rerun()

    st.divider()

    # Recent jobs
    st.subheader("🆕 Recent Jobs")
    jobs = get_all_jobs(limit=20)
    for job in jobs[:10]:
        with st.container():
            col_a, col_b, col_c = st.columns([4, 1, 1])
            with col_a:
                st.write(f"**{job['title']}**")
                st.caption(f"{job.get('company_name', job.get('site', '?'))} · {job.get('location', '')}")
            with col_b:
                st.write(score_badge(job.get("fit_score")))
            with col_c:
                st.write(f"via {job.get('site', '?')}")
            st.divider()

# ── Page: Jobs ───────────────────────────────────────────────────────────────

def page_jobs():
    st.title("💼 Job Bank")

    # Filters
    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        min_score_filter = st.slider("Minimum Score", 1, 10, MIN_SCORE_DEFAULT)
    with col2:
        site_filter = st.selectbox("Source", ["All", "linkedin", "indeed", "glassdoor", "google", "workday"])
    with col3:
        search = st.text_input("🔍 Search jobs", placeholder="Filter by title...")

    all_jobs = get_all_jobs(limit=500)

    # Apply filters
    if site_filter != "All":
        all_jobs = [j for j in all_jobs if j.get("site") == site_filter]
    if search:
        all_jobs = [j for j in all_jobs if search.lower() in (j.get("title") or "").lower()]

    st.caption(f"Showing {len(all_jobs)} of {len(get_all_jobs(limit=500))} total jobs")

    # Score filter
    all_jobs = [j for j in all_jobs if (j.get("fit_score") or 0) >= min_score_filter]

    for job in all_jobs:
        score = job.get("fit_score")
        url = job.get("url", "")
        title = job.get("title", "Unknown")

        with st.container():
            cols = st.columns([1, 5, 2, 1])
            with cols[0]:
                st.write(score_badge(score))
            with cols[1]:
                st.markdown(f"**{title}**")
                st.caption(f"{job.get('location', '')} · {job.get('site', '')} · {job.get('salary', '')}")
                if url:
                    st.caption(f"[Apply URL]({url[:80]}...)" if len(url) > 80 else f"[Apply URL]({url})")
            with cols[2]:
                tailored = "✅ Tailored" if job.get("tailored_resume_path") else "⬜ Not tailored"
                cover = "✅ Cover" if job.get("cover_letter_path") else "⬜ No cover"
                st.caption(tailored)
                st.caption(cover)
            with cols[3]:
                if st.button("View", key=f"view_{url[:30]}"):
                    st.session_state["selected_job_url"] = url
                    st.switch_page("__main__")
            st.divider()

# ── Page: Job Detail ─────────────────────────────────────────────────────────

def page_job_detail():
    st.title("📋 Job Detail")

    selected = st.session_state.get("selected_job_url")
    if not selected:
        st.info("No job selected. Go to the Jobs page and click 'View' on a job.")
        if st.button("← Go to Jobs"):
            st.switch_page("__main__")
        return

    conn = get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (selected,)).fetchone()
    if not row:
        st.error("Job not found in database.")
        return

    cols = row.keys()
    job = dict(zip(cols, row))

    # Header
    st.header(job.get("title", "Unknown Title"))
    score = job.get("fit_score")
    st.write(f"**Company:** {job.get('company_name', job.get('site', '?'))}")
    st.write(f"**Location:** {job.get('location', 'N/A')}")
    st.write(f"**Source:** {job.get('site', 'N/A')}")
    st.write(f"**Score:** {score_badge(score)}")

    if job.get("salary"):
        st.write(f"**Salary:** {job['salary']}")

    tabs = st.tabs(["📝 Description", "🧠 Scoring", "📄 Resume", "💌 Cover Letter", "🚀 Apply"])

    # ── Description ────────────────────────────────────────────────────────
    with tabs[0]:
        if job.get("full_description"):
            st.markdown(job["full_description"][:5000])
        elif job.get("description"):
            st.markdown(job["description"][:5000])
        else:
            st.info("No description available yet. Run the enrich stage.")

        st.divider()
        if job.get("application_url"):
            st.markdown(f"**Application URL:** [{job['application_url'][:80]}]({job['application_url']})")
        if job.get("url"):
            st.markdown(f"**Original URL:** [{job['url'][:80]}]({job['url']})")

    # ── Scoring ────────────────────────────────────────────────────────────
    with tabs[1]:
        if job.get("fit_score") is not None:
            st.metric("Fit Score", job["fit_score"])
        else:
            st.info("Not scored yet.")

        if job.get("score_reasoning"):
            st.markdown("**Reasoning:**")
            st.code(job["score_reasoning"], language="markdown")

        if st.button("♻️ Re-score this job"):
            st.info("Re-scoring individual jobs is not yet supported. Run the full score stage.")
            if st.button("▶ Run score stage"):
                with st.spinner("Running score stage..."):
                    output = run_pipeline_stage("score")
                    st.code(output[-3000:], language="bash")
                    st.rerun()

    # ── Resume ─────────────────────────────────────────────────────────────
    with tabs[2]:
        if job.get("tailored_resume_path"):
            path = Path(job["tailored_resume_path"])
            if path.exists():
                content = read_file(path)
                st.text_area("Tailored Resume", content, height=400, label_visibility="collapsed")
                st.download_button("📥 Download tailored resume", content, file_name=f"resume_{job['title'][:20]}.txt")
            else:
                st.warning(f"File not found: {path}")
        else:
            st.info("No tailored resume yet. Run the tailor stage.")
            if st.button("✂️ Tailor this resume"):
                st.info("Per-job tailoring not yet supported. Run the full tailor stage.")

    # ── Cover Letter ───────────────────────────────────────────────────────
    with tabs[3]:
        if job.get("cover_letter_path"):
            path = Path(job["cover_letter_path"])
            if path.exists():
                content = read_file(path)
                st.text_area("Cover Letter", content, height=400, label_visibility="collapsed")
                st.download_button("📥 Download cover letter", content, file_name=f"cover_{job['title'][:20]}.txt")
            else:
                st.warning(f"File not found: {path}")
        else:
            st.info("No cover letter yet. Run the cover stage.")
            if st.button("✍️ Generate cover letter"):
                st.info("Per-job cover letter generation not yet supported. Run the full cover stage.")

    # ── Apply ──────────────────────────────────────────────────────────────
    with tabs[4]:
        if job.get("applied_at"):
            st.success(f"✅ Applied on {job['applied_at']}")
            if job.get("apply_status"):
                st.write(f"**Status:** {job['apply_status']}")
            if job.get("apply_error"):
                st.error(f"**Error:** {job['apply_error']}")
        else:
            if not job.get("tailored_resume_path"):
                st.warning("⚠️ Need a tailored resume before applying. Run tailor + cover first.")
            elif not job.get("cover_letter_path"):
                st.warning("⚠️ Need a cover letter before applying. Run cover stage first.")
            else:
                st.success("✅ Ready to apply")

            if job.get("application_url"):
                st.markdown(f"**Apply at:** [{job['application_url'][:80]}]({job['application_url']})")

            if st.button("🚀 Submit Application", type="primary"):
                st.info("Auto-apply with Ollama agent is not yet built. The application URL is ready for manual application.")

# ── Page: Pipeline ───────────────────────────────────────────────────────────

def page_pipeline():
    st.title("⚙️ Pipeline Control")

    st.subheader("Run Pipeline Stages")

    cols = st.columns(3)
    stage_info = [
        ("1️⃣ Discover", "Find new jobs from LinkedIn, Indeed, Glassdoor, and Workday portals", "discover"),
        ("2️⃣ Enrich", "Extract full job descriptions and application URLs", "enrich"),
        ("3️⃣ Score", "Rate each job 1-10 using Ollama LLM against your resume", "score"),
        ("4️⃣ Tailor", "Rewrite resume bullets to match job description keywords (score ≥7)", "tailor"),
        ("5️⃣ Cover Letter", "Generate personalised cover letters for tailored jobs", "cover"),
        ("6️⃣ PDF Export", "Convert all tailored resumes and cover letters to PDF", "pdf"),
    ]

    for i, (label, desc, stage) in enumerate(stage_info):
        with cols[i % 3]:
            st.subheader(label)
            st.caption(desc)
            if st.button(f"▶ Run {stage}", type="primary", key=f"run_{stage}"):
                with st.spinner(f"Running {stage}..."):
                    output = run_pipeline_stage(stage)
                    st.code(output[-3000:] if len(output) > 3000 else output, language="bash")
                    st.rerun()

    st.divider()

    # Run all stages
    st.subheader("🚀 Run All Stages")
    st.write("Run the complete pipeline: discover → enrich → score → tailor → cover → pdf")
    if st.button("▶ Run Full Pipeline", type="primary"):
        stages = ["discover", "enrich", "score", "tailor", "cover", "pdf"]
        progress = st.progress(0)
        for i, stage in enumerate(stages):
            with st.spinner(f"Running {stage}..."):
                output = run_pipeline_stage(stage)
            progress.progress((i + 1) / len(stages), text=f"{stage} complete")
        st.success("Pipeline complete!")
        st.rerun()

    st.divider()

    # Ollama status
    st.subheader("🧠 Ollama Status")
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:11434/v1/chat/completions",
            method="POST"
        )
        req.add_header("Content-Type", "application/json")
        import json
        data = json.dumps({"model": "gemma4:31b-cloud", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}).encode()
        req.add_header("Content-Length", str(len(data)))
        with urllib.request.urlopen(req, data=data, timeout=5) as resp:
            result = json.loads(resp.read())
            st.success(f"Ollama OK: {result['choices'][0]['message']['content']}")
    except Exception as e:
        st.error(f"Ollama not responding: {e}")

# ── Page: Settings ───────────────────────────────────────────────────────────

def page_settings():
    st.title("⚙️ Settings")

    tabs = st.tabs(["👤 Profile", "🔍 Searches", "🤖 LLM Config", "📁 Files"])

    # ── Profile ─────────────────────────────────────────────────────────────
    with tabs[0]:
        st.subheader("profile.json")
        profile_path = Path("/home/bostjan/.applypilot/profile.json")
        if profile_path.exists():
            content = profile_path.read_text()
            edited = st.text_area("Edit profile.json", content, height=500)
            if st.button("💾 Save Profile"):
                profile_path.write_text(edited)
                st.success("Profile saved!")
        else:
            st.error("profile.json not found at ~/.applypilot/profile.json")

        st.divider()
        st.subheader("Work Experience")
        import json
        try:
            profile = json.loads(profile_path.read_text())
            for job in profile.get("work_experience", []):
                st.write(f"**{job.get('title')}** at {job.get('company')} ({job.get('start_date', '?')} – {job.get('end_date', 'now')})")
                for bullet in job.get("bullets", []):
                    st.write(f"  • {bullet}")
        except:
            pass

    # ── Searches ────────────────────────────────────────────────────────────
    with tabs[1]:
        st.subheader("searches.yaml")
        searches_path = Path("/home/bostjan/.applypilot/searches.yaml")
        if searches_path.exists():
            content = searches_path.read_text()
            edited = st.text_area("Edit searches.yaml", content, height=400)
            if st.button("💾 Save Searches"):
                searches_path.write_text(edited)
                st.success("searches.yaml saved!")
        else:
            st.error("searches.yaml not found")

        st.divider()
        st.subheader("Job Boards Enabled")
        boards = ["linkedin", "indeed", "glassdoor", "google"]
        for b in boards:
            st.write(f"  • {b}")

    # ── LLM Config ─────────────────────────────────────────────────────────
    with tabs[2]:
        st.subheader(".env — LLM Configuration")
        env_path = Path("/home/bostjan/.applypilot/.env")
        if env_path.exists():
            content = env_path.read_text()
            edited = st.text_area("Edit .env", content, height=200)
            if st.button("💾 Save .env"):
                env_path.write_text(edited)
                st.success(".env saved! Restart pipeline for changes to take effect.")
        else:
            st.error(".env not found")

        st.divider()
        st.subheader("Available Ollama Models")
        try:
            import urllib.request, json
            req = urllib.request.Request("http://127.0.0.1:11434/api/tags", headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                models = json.loads(resp.read()).get("models", [])
                for m in models:
                    st.write(f"  • {m.get('name', '?')}")
        except Exception as e:
            st.warning(f"Could not fetch models: {e}")

    # ── Files ───────────────────────────────────────────────────────────────
    with tabs[3]:
        st.subheader("ApplyPilot Directory")
        app_dir = Path("/home/bostjan/.applypilot")
        for item in sorted(app_dir.iterdir()):
            size = item.stat().st_size if item.is_file() else "-"
            st.write(f"  {'📁' if item.is_dir() else '📄'} {item.name} ({size} bytes)")

        st.divider()
        st.subheader("Generated Outputs")
        st.write(f"**Cover Letters:** {COVER_DIR}")
        st.write(f"**Tailored Resumes:** {TAILORED_DIR}")
        st.write(f"**Database:** {DB}")

        cover_count = len(list(COVER_DIR.glob("*"))) if COVER_DIR.exists() else 0
        tailored_count = len(list(TAILORED_DIR.glob("*"))) if TAILORED_DIR.exists() else 0
        st.metric("Cover letters generated", cover_count)
        st.metric("Tailored resumes generated", tailored_count)

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Navigation
    pages = {
        "📊 Dashboard": page_dashboard,
        "💼 Jobs": page_jobs,
        "📋 Job Detail": page_job_detail,
        "⚙️ Pipeline": page_pipeline,
        "⚙️ Settings": page_settings,
    }

    # Auto-switch to job detail if we have a selected job
    if st.session_state.get("selected_job_url") and st.query_params.get("page") != "jobs":
        st.switch_page(page_job_detail)

    st.sidebar.title("🎯 ApplyPilot")
    st.sidebar.caption("AI-powered job application automation")

    selection = st.sidebar.radio("Navigate", list(pages.keys()), index=0)

    if selection == "📋 Job Detail" and not st.session_state.get("selected_job_url"):
        selection = "📊 Dashboard"

    pages[selection]()

if __name__ == "__main__":
    main()