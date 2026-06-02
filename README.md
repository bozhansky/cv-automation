# ApplyPilot — Boštjan's AI Prompting Job Search Automation

This document explains how to use the ApplyPilot system to automate job discovery, AI-powered scoring, resume tailoring, cover letter generation, and auto-apply — specifically tuned for finding AI / LLM / Prompt Engineering roles across the EU.

---

## Table of Contents

1. [How the Pipeline Works](#1-how-the-pipeline-works)
2. [Adding Job Search Resources](#2-adding-job-search-resources)
3. [Configuration Files](#3-configuration-files)
4. [Running the Pipeline](#4-running-the-pipeline)
5. [Web UI (Streamlit)](#5-web-ui-streamlit)
6. [Auto-Apply Agent](#6-auto-apply-agent)
7. [Maintenance: Purge, Cron, DB Indexes](#7-maintenance-purge-cron-db-indexes)
8. [Troubleshooting](#8-troubleshooting)
9. [Finding AI Prompting Jobs — Strategy Guide](#9-finding-ai-prompting-jobs--strategy-guide)

---

## 1. How the Pipeline Works

ApplyPilot runs in seven sequential stages. Each stage reads from and writes to a local SQLite database (`~/.applypilot/applypilot.db`).

```
┌──────────────────────────────────────────────────────────────────────┐
│  STAGE 1: discover                                                   │
│  Scrapes job boards (LinkedIn, Indeed, Glassdoor, Google Jobs) and   │
│  Workday employer portals for matching job postings.                  │
│  Output: ~50-200 raw job entries (URL, title, company, location)     │
│  Storage: applypilot.db "jobs" table                                 │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────────┐
│  STAGE 2: employers                                                  │
│  Scrapes career pages from 18 target companies listed in            │
│  employers.yaml — supplements JobSpy with direct company sources.    │
│  Output: additional job entries from employer career pages           │
│  Storage: applypilot.db "jobs" table                                 │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────────┐
│  STAGE 3: enrich                                                      │
│  Visits each job URL to extract the full job description, salary,   │
│  requirements, and the direct application link.                     │
│  Output: Full text descriptions for all discovered jobs              │
│  Storage: applypilot.db "jobs" table (updated in place)              │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────────┐
│  STAGE 4: score                                                       │
│  Sends each job + your resume text to the LLM (Ollama) for scoring.  │
│  Assigns a fit score 1-10 based on role alignment, skills match,      │
│  location fit, and experience level.                                  │
│  Output: fit_score + reasoning for every job                         │
│  Storage: applypilot.db "jobs" table (fit_score, score_reasoning)    │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────────┐
│  STAGE 5: tailor                                                      │
│  For jobs scoring ≥ 7: rewrites your resume bullets to match the     │
│  job description keywords and phrasing.                              │
│  Validation: checks for banned words, alignment with your profile.   │
│  Output: tailored_resume/ directory with .txt files                  │
│  Storage: applypilot.db (tailored_resume_path per job)               │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────────┐
│  STAGE 6: cover                                                       │
│  Generates a personalised cover letter for each tailored job.       │
│  Each letter references specific requirements from the job posting.   │
│  Output: cover_letters/ directory with .txt files                    │
│  Storage: applypilot.db (cover_letter_path per job)                  │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────────┐
│  STAGE 7: apply                                                       │
│  Ollama-powered Playwright automation with user approval gate.     │
│  Polls for jobs with tailored resume + cover letter ready to apply. │
│  Navigates to application URL, pre-fills form via Playwright.       │
│  User confirms in the Streamlit UI before form is submitted.         │
│  Telegram notification sent on submission.                           │
│  Status: applypilot.db updated (applied_at, apply_status)           │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2. Adding Job Search Resources

ApplyPilot supports three types of job sources. You configure all of them through files in `~/.applypilot/`.

### 2a. Job Board Searches (LinkedIn, Indeed, Glassdoor, Google, ZipRecruiter)

Edit `~/.applypilot/searches.yaml`. This controls what keywords and locations JobSpy searches on each board.

**Adding search queries** — add a new entry under `queries`:

```yaml
queries:
  - query: "your search term here"
    tier: 1          # tier 1 = most important (scored first)
  - query: "secondary search term"
    tier: 2          # tier 2 = backup if tier 1 returns nothing
```

**Adding locations** — edit the `locations` section:

```yaml
locations:
  - location: "Germany"
    remote: true     # force remote results
  - location: "Remote - EU"
    remote: true
```

**Enabling/disabling boards** — change the `boards` list:

```yaml
boards:
  - indeed           # ✓ enabled
  - linkedin         # ✓ enabled
  - glassdoor        # ✓ enabled
  - google           # ✓ enabled
  # - zip_recruiter  # ✗ disabled (currently returns 403)
```

**Location filtering** — `location.accept_patterns` and `reject_patterns` filter what jobs are stored based on their text. Add new countries:

```yaml
location:
  accept_patterns:
    - "Remote"
    - "Europe"
    - "Slovenia"
    - "Germany"
    - "Netherlands"
    - "Sweden"
    - "Anywhere"
  reject_patterns:
    - "onsite only"
    - "India"
    - "Philippines"
```

### 2b. Workday Employer Portals

Workday hosts career pages for large enterprises. ApplyPilot can scrape these directly. Currently configured with Canadian/US companies (TD Bank, RBC, BMO, etc.).

**To add a new Workday employer:**

1. Find their Workday career URL: `https://[company].wd[N].myworkdayjobs.com`
   Example: `https://sap.wd5.myworkdayjobs.com` for SAP

2. Edit `~/.applypilot/config/employers.yaml` (create it if it doesn't exist):

```yaml
employers:
  sap:
    name: "SAP"
    tenant: "sap"
    site_id: "Search"        # usually "Search" or check the URL path
    base_url: "https://sap.wd5.myworkdayjobs.com"
```

3. In `searches.yaml`, the Workday scraper automatically uses all queries defined in the `queries` list and filters results by your location accept/reject patterns.

### 2c. Smart Job Extractors (direct career site scraping)

ApplyPilot also includes `smartextract.py` which tries to detect and scrape direct career page URLs when you provide them. Configure in `~/.applypilot/config/sites.yaml`:

```yaml
sites:
  - name: "Company Name"
    career_url: "https://company.com/careers"
    job_path: "/careers/jobs/"     # optional: URL pattern for job listings
    apply_url_pattern: ""          # optional: override apply URL detection
```

---

## 3. Configuration Files

All configuration lives in `~/.applypilot/`. Here's what each file does:

| File | Purpose |
|------|---------|
| `profile.json` | Your personal details — name, email, phone, work history, education, skills, languages, salary expectations. This is the primary input used for tailoring resumes and cover letters. |
| `searches.yaml` | Search queries, locations, board selection, and location filtering rules. |
| `.env` | Environment variables — LLM endpoint URL, model name, API key. |
| `resume.txt` | Plain-text version of your CV. Auto-extracted from `resume.pdf`. |
| `resume.pdf` | Your primary CV PDF (symlink from your workspace). |
| `config/employers.yaml` | Workday employer registry (list of companies + their Workday URLs). |
| `config/sites.yaml` | Direct career site URLs for smart job extraction. |
| `applypilot.db` | SQLite database — all discovered jobs, scores, tailored paths, apply status. |

### `profile.json` — Fields You Can Update

- `personal.*` — name, email, phone, address, LinkedIn URL
- `summary` — 2-3 sentence professional summary
- `skills[]` — list of your core skills (used for tailoring)
- `work_experience[]` — job history (company, title, dates, bullets)
- `education[]` — degrees and institutions
- `languages[]` — with proficiency levels
- `compensation.salary_range_min/max` — EUR range for your target
- `work_authorization.work_permit_type` — "EU citizen (Slovenia)" etc.

### `.env` — LLM Configuration

```bash
LLM_PROVIDER=openai                    # Tell ApplyPilot to use OpenAI-compatible API
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1   # Your local Ollama server
LLM_URL=http://127.0.0.1:11434/v1             # Alternative key (unlocks Tier 3)
LLM_MODEL=gemma4:31b-cloud              # Model to use (see available models below)
LLM_API_KEY=***                     # Any string (Ollama doesn't need real keys)
```

**Available Ollama models** (use `ollama list` to see yours):
- `gemma4:31b-cloud` — recommended, 31B params via Ollama Cloud
- `deepseek-v4-flash:cloud` — fast, good for structured tasks
- `qwen3.5:cloud` — strong reasoning
- `kimi-k2.6:cloud` — used by the auto-apply agent (best tool-calling)
- `minimax-m2.7:cloud`

### AI-Signal Sanitizer (auto-cleanup of LLM output)

Every LLM response in ApplyPilot passes through a post-processor (`applypilot/text_sanitizer.py`) that removes the writing patterns that mark text as AI-generated. It is wired into the LLM client at both return points (OpenAI-compat + native Gemini), so **all 4 LLM call sites benefit automatically**: scorer, tailor (×2), cover_letter.

What it strips:
- Curly quotes (`""''`) → straight quotes (`"`)
- Em-dash (`—`) and en-dash (`–`) → hyphen (`-`)
- AI clichés: "delve", "glimpse", "stark", "In today's world", "Needless to say", "It's not just X, it's also Y", "leverage", "seamless", "robust", and ~100 others
- Idea repetition (consecutive duplicate sentences dropped)
- Keyword stuffing (multiple `, , ,`, `!!!`, `?!?!`, etc.)
- Bias injection: when text reads too neutral, a deterministic humanizing closing sentence is appended (cover letters only — resumes are unaffected because they parse JSON)

Toggles (env vars):
- `APPLY_NO_SANITIZE=1` — bypass all cleaning (debug only)
- `APPLY_NO_BIAS=1` — skip the humanizing-tail injection (cover-letter only)

---

## 4. Running the Pipeline

All commands use `python3 -m applypilot`. Set `HOME=/home/bostjan` first since that's where the config lives.

### Full pipeline (all stages)

```bash
export HOME=/home/bostjan
python3 -m applypilot run
```

### Individual stages

```bash
export HOME=/home/bostjan

# Stage 1: discover new jobs
python3 -m applypilot run discover

# Stage 2: scrape employer career pages
python3 -m applypilot run employers

# Stage 3: enrich job descriptions
python3 -m applypilot run enrich

# Stage 4: score all jobs (requires LLM)
python3 -m applypilot run score

# Stage 5: tailor resumes for jobs scoring ≥7
python3 -m applypilot run tailor

# Stage 6: generate cover letters for tailored jobs
python3 -m applypilot run cover

# Stage 7: auto-apply (Playwright + Ollama, approval-gated)
python3 -m applypilot run apply

# Stages 3-5 chained
python3 -m applypilot run score tailor cover

# All stages including auto-apply
python3 -m applypilot run all
```

### Options

| Flag | Purpose |
|------|---------|
| `--dry-run` | Preview what would run without executing |
| `--min-score N` | Override minimum fit score for tailor/cover stages (default: 7) |
| `-w N` | Parallel workers for discovery/enrichment stages (default: 1) |
| `--stream / --no-stream` | Run stages concurrently (default: enabled, ~3-4x faster) |
| `--since {24h\|7d\|ISO}` | Only process jobs discovered at/after this window (e.g. `24h`, `7d`, `2026-06-01T20:00:00`) |
| `--url TEXT` | Apply to a specific job URL |
| `--limit N` | Limit number of jobs to process |
| `--headless` | Run browser in headless mode (auto-apply only) |

### Checking status

```bash
export HOME=/home/bostjan
python3 -m applypilot status       # show DB counts
python3 -m applypilot dashboard   # open web dashboard (Streamlit)
```

### On-Demand Single-Job Commands

For re-running individual stages on a single job without redoing the whole pipeline:

```bash
export HOME=/home/bostjan LLM_URL="http://127.0.0.1:11434/v1" \
       LLM_MODEL="gemma4:31b-cloud" LLM_API_KEY="not-needed"

# Re-tailor a single job by URL (bypasses score threshold)
python3 -m applypilot tailor "https://www.linkedin.com/jobs/view/4417252427"

# Re-generate cover letter for a single job
python3 -m applypilot cover  "https://www.linkedin.com/jobs/view/4417252427"

# Or pipeline a new URL not yet in the DB: discover -> score -> tailor -> cover
# (use the Streamlit Pipeline page for the GUI; CLI: `python3 -m applypilot run all --since 24h`)
```

### Purge Old Jobs

By default ApplyPilot runs a weekly cleanup that removes jobs discovered more than 7 days ago (and their tailored resume / cover letter files). Two preservation flags:

```bash
export HOME=/home/bostjan

# Dry-run preview
python3 -m applypilot purge --dry-run

# Real run (preserves applied + approved jobs)
python3 -m applypilot purge

# Custom threshold
python3 -m applypilot purge --older-than-days 14

# Also delete applied/approved jobs (destructive — only when archiving)
python3 -m applypilot purge --include-applied --include-approved
```

A row is preserved from purge if **any** of these are set:
- `applied_at IS NOT NULL` (you already submitted)
- `approved_at IS NOT NULL` (you explicitly approved it in the Streamlit UI)

### Cron Jobs (Automated)

Three cron jobs are configured to keep the pipeline running unattended:

| Schedule | What | Wrapper |
|---|---|---|
| `0 */4 * * *` (every 4h) | `discover` only, with flock lockfile | `~/.hermes/scripts/applypilot_discover.sh` |
| `0 20 * * *` (20:00 daily) | Full pipeline, last 24h window | `~/.hermes/scripts/applypilot_daily_pipeline.sh` |
| `0 3 * * 6` (Sat 03:00) | Weekly purge of jobs >7 days old | `~/.hermes/scripts/applypilot_weekly_purge.sh` |

All three log to `~/.applypilot/cron-*.log` and deliver a status summary to the chat.

To inspect or pause: use the `cronjob` tool, or `crontab -l` (the system crontab is empty — Hermes manages these).

---

## 5. Web UI (Streamlit)

A full Streamlit frontend is available at `frontend/app.py`. Start it with:

```bash
cd /media/bostjan/Documents/Osebno/ZAPOSLITEV/AI\ JOB\ 2026
streamlit run frontend/app.py
```

Or use the provided launcher:
```bash
./run_frontend.sh
```

**Pages:**
- **Dashboard** — pipeline stats, pending approvals, recent jobs
- **Jobs** — filterable job bank with score/status badges
- **Job Detail** — per-job view with description, scoring, tailored resume, cover letter, and apply controls
- **Pipeline** — run any stage manually with live output. Also has three sub-sections:
  - **🚀 Run Whole Pipeline** — one-click `run all` with min-score, `--since` window, and confirm checkbox
  - **🎯 On-Demand: Tailor / Cover a Single Job** — paste a URL and trigger tailor/cover for one job
  - **🗑️ Purge Old Jobs** — UI for the weekly purge with days/applied/approved controls
- **Settings** — edit profile.json, searches.yaml, auto-search interval, Telegram config

### Dashboard approval & preservation

When a job has a tailored resume + cover letter, it appears in the Dashboard's **Pending Approvals** section. Clicking **Approve** sets `apply_status='approved'` and `approved_at=NOW`, which excludes the row from weekly purge. Clicking **Decline** sets `apply_status='declined'` (the row will be eligible for purge).

---

## 6. Auto-Apply Agent

The auto-apply agent (`agents/auto_apply.py`) uses Playwright automation powered by Ollama reasoning, with a user approval gate before any form is submitted.

**Approval flow:**
1. Jobs with tailored resume + cover letter are queued as `pending_approval`
2. Telegram notification sent to `@bozhoapplybot` with job details
3. User opens the Streamlit Dashboard and clicks **Approve** or **Decline**
4. On approval: Playwright navigates to the application URL, pre-fills common fields (name, email, phone), uploads the tailored resume PDF, pastes the cover letter text
5. User completes and submits the form manually in the browser
6. On submit: DB updated to `applied`, Telegram confirmation sent

**Playwright automation:**
- Runs via Node.js (`playwright` npm package)
- Launches Chrome browser in non-headless mode by default (so user can review before submitting)
- Uses Ollama `gemma4:31b-cloud` to reason through form field identification
- Pre-fills name/email/phone from `profile.json`, uploads resume, pastes cover letter up to 2000 chars
- Result logged to `applypilot.db` with `applied_at` timestamp

**Telegram integration:**
- Bot: `@bozhoapplybot` (token configured in `~/.applypilot/.env`)
- Chat ID: `7003890359`
- Notifications sent on: job queued for approval, application submitted

---

## 7. Maintenance: Purge, Cron, DB Indexes

### Database indexes

Three indexes are auto-created on `init_db()`:
- `idx_jobs_discovered_at` — speeds up `--since` filters and the weekly purge
- `idx_jobs_apply_status` — speeds up "ready to apply" / "approved" set queries
- `idx_jobs_fit_score` — speeds up the "high-score jobs" view

You don't need to do anything. If you ever see a fresh DB without them, run:

```bash
export HOME=/home/bostjan && python3 -c "from applypilot.database import ensure_indexes; print(ensure_indexes())"
```

### Schema migrations

The jobs table has 26 columns. When new ones are added (e.g. `approved_at`), `ensure_columns()` runs on every `init_db()` and uses `ALTER TABLE ADD COLUMN` to bring old DBs forward. **No data is ever destroyed by a migration.**

### URL canonicalization (dedup)

`store_jobs()` runs every URL through `_canonicalize_url()` before insert. Tracking query params are stripped: `utm_*`, `trk`, `ref`, `vjk`, `fromage`, `gclid`, `fbclid`, `_ga`, `_gl`, `mc_cid`, `mc_eid`, etc. Essential params (Indeed `jk`, LinkedIn `currentJobId`) are kept. This collapses LinkedIn `?trk=...` and Indeed `?vjk=...` variants of the same job into a single row.

### Watchdog (orphan-process prevention)

The 4-hourly discover cron uses `flock -n` on `/tmp/applypilot_discover.lock` to prevent stacking. If a previous discover is still running, the new tick exits immediately (logs "Another discover is already running — skipping"). The lock auto-releases when the process exits (no stale lockfiles).

### Manual intervention

```bash
# Force re-score all jobs (clears fit_score)
sqlite3 /home/bostjan/.applypilot/applypilot.db \
  "UPDATE jobs SET fit_score=NULL, score_reasoning=NULL, scored_at=NULL"

# Count jobs by apply_status
sqlite3 /home/bostjan/.applypilot/applypilot.db \
  "SELECT apply_status, COUNT(*) FROM jobs WHERE apply_status IS NOT NULL GROUP BY apply_status"

# Tailored-but-not-yet-applied (the "ready to apply" queue)
sqlite3 /home/bostjan/.applypilot/applypilot.db \
  "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"

# Approved-but-not-yet-applied
sqlite3 /home/bostjan/.applypilot/applypilot.db \
  "SELECT COUNT(*) FROM jobs WHERE approved_at IS NOT NULL AND applied_at IS NULL"
```

---

## 8. Troubleshooting

### "No module named 'jobspy'"

Two packages named `jobspy` exist. You need `python-jobspy`:
```bash
pip3 install --no-deps python-jobspy --break-system-packages
pip3 install tls-client requests markdownify regex --break-system-packages
```

### "Tier 1 — Discovery only" / Missing LLM API key

ApplyPilot can't detect your LLM. Add `LLM_URL` to `~/.applypilot/.env`:
```bash
LLM_URL=http://127.0.0.1:11434/v1
```
Then re-run with `export HOME=/home/bostjan && python3 -m applypilot run`.

### "No such file: resume.txt"

The score stage needs a plain-text resume at `~/.applypilot/resume.txt`. If missing, extract it:
```bash
pdftotext /home/bostjan/.applypilot/resume.pdf /home/bostjan/.applypilot/resume.txt
```

### Jobs not appearing for my search queries

1. Check `searches.yaml` location patterns — jobs whose location text doesn't match `accept_patterns` are filtered out before storage
2. Try adding "Remote" explicitly in the location field
3. Run with `--dry-run` to see what would be searched
4. Check if the board is blocked (ZipRecruiter currently returns 403 — disable it in `boards:`)

### Ollama not responding

```bash
curl -s http://127.0.0.1:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemma4:31b-cloud","messages":[{"role":"user","content":"ping"}],"max_tokens":5}'
```
Should return `{"choices":[{"message":{"content":"Pong!"}}...]}`.

### Too many US/Canadian jobs

Your `searches.yaml` location filtering may be too loose. Ensure `location.accept_patterns` includes "Remote", "Europe", "EU", and your target countries. Add explicit country names. Also set `country: "de"` (Germany) or `country: "si"` (Slovenia) in `searches.yaml` — this controls the country bias of job board results.

### Re-scoring all jobs from scratch

Delete scores in the database and re-run:
```bash
export HOME=/home/bostjan
python3 -c "
import sqlite3
db = sqlite3.connect('/home/bostjan/.applypilot/applypilot.db')
db.execute('UPDATE jobs SET fit_score=NULL, score_reasoning=NULL, scored_at=NULL')
db.commit()
"
python3 -m applypilot run score
```

### Streamlit frontend won't start

Make sure streamlit is installed:
```bash
pip3 install streamlit --break-system-packages
```
Check that the working directory is the project root when running `streamlit run`.

---

## 9. Finding AI Prompting Jobs — Strategy Guide

Your profile has a strong combination of **business analysis + AI/ML skills**. Here's how to position yourself and get the most from ApplyPilot.

### Your Best Job Titles (use in searches.yaml)

| Category | Titles to search |
|----------|-----------------|
| Core prompt engineering | `prompt engineer`, `AI engineer`, `LLM engineer`, `AI specialist` |
| AI + business | `AI product analyst`, `AI implementation consultant`, `AI solutions architect` |
| AI + SaaS | `GenAI consultant SaaS`, `AI integration specialist`, `AI customer success engineer` |
| AI operations | `AI operations specialist`, `AI data analyst`, `machine learning operations` |

### How to Position Your Profile

**Strengths to emphasise:**
- 18+ years ICT market research → you understand domain complexity and can write prompts that capture nuanced requirements
- Executive MBA + quantitative skills → you can evaluate AI outputs critically
- Vanderbilt AI certifications (5 completed, 2 in progress) → proven AI learning trajectory
- Vibe coding, agentic AI, LLM integration (Ollama, ChatGPT, Claude) → hands-on technical skills
- B2B sales + consultative selling → you can communicate AI value to non-technical stakeholders

**Key differentiator:** You're NOT a pure developer. You're a business-aware AI practitioner who can both use LLMs effectively AND translate AI capabilities into business outcomes. Roles like AI product analyst, AI implementation consultant, and AI solutions architect value exactly this combination.

### Optimising Your Searches

Edit `~/.applypilot/searches.yaml` to add these high-signal queries:

```yaml
queries:
  # ── Tier 1: Your core target roles ─────────────────────────────────
  - query: "prompt engineer generative AI Europe remote"
    tier: 1
  - query: "AI engineer LLM GPT Europe remote"
    tier: 1
  - query: "prompt engineering specialist AI"
    tier: 1
  - query: "AI product analyst GPT LLM"
    tier: 1
  - query: "AI operations specialist Europe"
    tier: 1

  # ── Tier 2: AI + business combo (your sweet spot) ───────────────────
  - query: "AI implementation consultant B2B SaaS"
    tier: 2
  - query: "AI solutions architect business analyst Europe"
    tier: 2
  - query: "LLM evaluation engineer Europe remote"
    tier: 2
  - query: "AI customer success engineer"
    tier: 2
  - query: "GenAI consultant B2B SaaS Europe"
    tier: 2

  # ── Tier 3: AI adjacent ─────────────────────────────────────────────
  - query: "AI product manager generative AI"
    tier: 3
  - query: "automation engineer AI workflows Europe"
    tier: 3
```

### Location Settings (already configured)

Your `searches.yaml` uses `country_indeed: "slovenia"` and `location_accept` patterns for EU countries. Keep `remote: true` on EU locations. Do NOT add US/Canada locations — your salary expectations and work authorisation point to EU roles.

### What to Run and When

| Action | Command | Frequency |
|--------|---------|-----------|
| Find new jobs | Click **🔍 Check for New Jobs** on Dashboard or `python3 -m applypilot run discover` | Daily |
| Score new jobs | `python3 -m applypilot run score` | After each discover |
| Tailor resumes | `python3 -m applypilot run tailor` | Weekly |
| Generate cover letters | `python3 -m applypilot run cover` | After tailor |
| Auto-apply | `python3 -m applypilot run apply` | As needed |

### Auto-Apply Approval Flow

1. Jobs with tailored resume + cover letter appear in Dashboard with **Approve / Decline** buttons
2. Telegram notification sent to `@bozhoapplybot` when a job is queued
3. You review in Streamlit and click **Approve** — browser opens with form pre-filled
4. You review the pre-fill and submit manually
5. DB updated → Telegram confirmation sent

### LinkedIn Profile Tips

Your LinkedIn headline should clearly communicate your AI focus. Consider updating to something like:
> **AI & Business Analyst | Prompt Engineering, GenAI, LLM Integration | 18+ yrs ICT Market Research**

Your About section should emphasise: prompt engineering skills, Vanderbilt AI certifications, hands-on experience with ChatGPT/Claude/Ollama, and your business analysis background.

To fetch and review your LinkedIn profile data, click **📋 Fetch My LinkedIn Profile** in the Settings page (Chrome must be running with CDP on port 9222).

### Salary Expectations for AI Prompting Roles in EU

Based on your experience and location (Slovenia/EU):
- **Entry-level prompt engineering**: €35,000–55,000
- **Mid-level AI engineer/prompt specialist**: €50,000–75,000
- **Senior AI product analyst / consultant**: €65,000–90,000
- **AI solutions architect / LLM engineer**: €80,000–110,000+

Your profile.json is set with `salary_range_min: 40000` and `salary_range_max: 100000` — this is reasonable. Adjust upward if targeting senior roles.

### Key Companies to Target (EU AI Prompting)

| Company Type | Examples |
|-------------|---------|
| AI-native startups | Aleph Alpha (DE), Mistral (FR), Cohere (UK), Zhipu AI (UK) |
| Enterprise AI platforms | DataRobot, C3.ai, H2O.ai (all with EU offices) |
| Consulting firms | Accenture AI, Deloitte AI, Capgemini (EU AI practices) |
| SaaS companies with AI features | Salesforce (EU), HubSpot (EU), SAP (DE) |
| Market research + analytics | Gartner, Forrester, IDC (you already know these) |