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

### Pipeline controls in the Streamlit dashboard

The **Pipeline** page in the Streamlit dashboard has a top-of-page "🎛️ Pipeline controls" bar with three settings that apply to ALL per-stage buttons in that page:

| Control | What it does | Default |
|---|---|---|
| **Min fit score for tailor/cover** (slider, 0–10) | When you click ▶ Run `tailor` or ▶ Run `cover`, only jobs with `fit_score >= N` are processed. Set to 10 to only rewrite resumes for the very best matches. | 0 (no filter) |
| **Only process jobs discovered in…** (selectbox) | Same as CLI `--since`. Useful for daily cron runs so you don't re-tailor jobs from previous days. Options: `(no filter)`, `Last 24h`, `Last 7d`, `Last 30d`. | `(no filter)` |
| **Parallel workers** (1–8) | Same as CLI `-w`. Number of parallel threads for `discover` and `enrich` stages. | 1 |

The controls translate to the corresponding CLI flags (`--min-score`, `--since`, `--workers`) and are passed through to `run_stage()` which then shells out to `applypilot run`. So the dashboard is just a UI on top of the CLI — same logic, same validation, same AI-signal sanitizer.

The pipeline controls also show a live counter: e.g. "📊 Currently filtering to **score ≥ 9**. 14 jobs in the DB match." so you can see at a glance how many jobs the threshold will process before you click Run.

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

# Build a combined resume+cover PDF packet for one job
python3 -m applypilot packet "https://www.linkedin.com/jobs/view/4417252427"

# Or pipeline a new URL not yet in the DB: discover -> score -> tailor -> cover
# (use the Streamlit Pipeline page for the GUI; CLI: `python3 -m applypilot run all --since 24h`)
```

**Note:** The on-demand `tailor` / `cover` subcommands call the per-job functions directly, not the batch `run_tailoring()` / `run_cover_letters()`. They use the same LLM prompts, validator, and AI-signal sanitizer, but skip the batch loop's metadata tracking. New validation rules added to the batch path won't auto-apply to on-demand — keep the batch path as the source of truth.

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

### Cron Jobs (Automated) — systemd timers

The pipeline runs unattended via **user-mode systemd timers** (not the legacy Hermes cron system, which had an import error that prevented the scripts from executing). Three timers cover the same schedule that the Hermes crons used to:

| Schedule | Service | What it does |
|---|---|---|
| Every 4 hours (`00,04,08,12,16,20:00`) | `applypilot-discover.service` | `discover` stage only, with `flock` lockfile to prevent stacking |
| Daily at 20:00 | `applypilot-daily.service` | Full pipeline (discover + enrich + score + tailor + cover), last-24h window |
| Saturdays at 03:00 | `applypilot-weekly-purge.service` | Weekly purge of jobs >7 days old |

**Why systemd and not Hermes cron?** The Hermes cron "jobs" are LLM agent sessions, not dumb shell runners. An unrelated import error in Hermes's tool backend (`cannot import name 'nous_tool_gateway_unavailable_message'`) was preventing the agent from even reaching the `terminal` tool to invoke the wrapper script. systemd timers run the wrapper directly — no agent, no prompt execution, no import surface to break.

**Why user-mode (`--user`) and not system?** The pipeline reads `~/.applypilot/` (the user's data dir) and `~/.hermes/scripts/`. Running as the user avoids `Permission denied` errors without `sudo`. The user has `Linger=yes` so timers run even when the user isn't logged in.

**Inspect and manage:**

```bash
# List all applypilot timers and their next run
systemctl --user list-timers 'applypilot*'

# Run a service right now (manual trigger — does not affect schedule)
systemctl --user start applypilot-discover.service

# Watch the live journal
journalctl --user -u applypilot-discover.service -f

# Disable a timer (e.g. when debugging)
systemctl --user disable --now applypilot-daily.timer

# Re-enable
systemctl --user enable --now applypilot-daily.timer
```

**Files:**

| Path | Purpose |
|---|---|
| `~/.config/systemd/user/applypilot-*.{service,timer}` | Unit files (live) |
| `scripts/applypilot_*.sh` | Same wrappers the timers call (mirrored to the repo for version control) |
| `~/.hermes/scripts/applypilot_*.sh` | Canonical wrappers (systemd units point here) |
| `~/.applypilot/cron-*.log` | Per-stage log file (write-target of the wrappers) |
| `journalctl --user -u applypilot-*` | systemd's own structured log (always preserved) |

The wrapper scripts are **dual-located** — the canonical copies in `~/.hermes/scripts/` are what systemd invokes, and identical copies live in the workspace `scripts/` so they're version-controlled. The wrappers themselves are simple bash that set env vars, run a health check, then exec the applypilot CLI.

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
- **Dashboard** — pipeline stats, pending approvals, **filterable job list with dates + per-row delete**
- **Jobs** — filterable job bank with score/status badges
- **Job Detail** — per-job view with description, scoring, tailored resume, cover letter, and apply controls
- **Pipeline** — run any stage manually with live output. Also has three sub-sections:
  - **🚀 Run Whole Pipeline** — one-click `run all` with min-score, `--since` window, and confirm checkbox
  - **🎯 On-Demand: Tailor / Cover a Single Job** — paste a URL and trigger tailor/cover for one job
  - **🗑️ Purge Old Jobs** — UI for the weekly purge with days/applied/approved controls
- **Site Analytics** — per-site success rate (4.9), dynamic blacklist (4.10), form schema cache (4.8), Telegram status (4.6)
- **Settings** — edit profile.json, searches.yaml, auto-search interval, Telegram config

### Dashboard filters + delete

The Dashboard's job list (under "🗂️ Jobs") supports filtering and per-row deletion:

**Filters** (open the 🔍 Filters expander):
- **Site** — dropdown of all sites in DB (default: all)
- **Title contains** — case-insensitive text search on job title
- **Discovered from / to** — date range for `discovered_at`
- **Applied from / to** — date range for `applied_at`
- **Fit score range** — slider 0-10
- **Only applied jobs** — checkbox to filter to `applied_at IS NOT NULL`
- **Max rows** — pagination control (default 100)
- **🔄 Reset filters** — clears all

**Per-row display** (in addition to title/site/location):
- 🔍 **Discovered**: full date + time
- ✅ **Applied**: full date + time (or `—` if not yet applied)
- Tailoring/cover status (✅/⬜)
- **View** button → Job Detail page
- **🗑️ Delete** button → 2-step confirmation → also deletes the tailored/cover PDFs from disk

**Filter SQL example** (for reference; the Streamlit code builds the same):
```sql
SELECT * FROM jobs
WHERE site = 'linkedin'
  AND LOWER(title) LIKE '%prompt engineer%'
  AND discovered_at >= '2026-05-01' AND discovered_at < '2026-06-01'
  AND fit_score BETWEEN 7 AND 10
  AND applied_at IS NOT NULL
ORDER BY fit_score DESC NULLS LAST, discovered_at DESC NULLS LAST
LIMIT 100;
```

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
- Bot: `@sluzhbot` (token configured in `~/.applypilot/.env` as `TELEGRAM_BOT_TOKEN`)
- Chat ID: `7003890359` (in same file as `TELEGRAM_CHAT_ID`)
- Notifications sent on: job queued for approval, application submitted, apply failed
- **Inline-keyboard approval flow** — see [§ 6.1 Telegram approval flow](#61-telegram-approval-flow) below

**Apply-stage safety rails (4.1–4.10):**

The apply agent is wrapped in several safety layers that fire BEFORE any Submit/Apply click. All failures are recorded as `apply_status` and surface in the Dashboard — nothing is silently dropped.

| Layer | What it catches | Config |
|---|---|---|
| **Preflight** (4.3) | Missing tailored PDF / cover PDF, 0-byte files, non-PDF header, bad URL, fit_score < 7, path outside data dir | `APPLY_SKIP_FILE_CHECK=1` to disable file checks |
| **Submit gate** (4.1) | Agent must call `browser_take_screenshot` immediately before `submit_application`. Other tool calls between screenshot and submit reset the gate. Also refuses in dry-run. | Built-in |
| **Dry-run DB protection** (4.4) | `--dry-run` jobs are marked `dry_run_ok` (not `applied`) so the DB stays clean | `--dry-run` flag |
| **Per-job cost cap** (4.2) | Aborts an apply turn if estimated cost exceeds `APPLY_MAX_COST_PER_JOB` (default $0.50, $3/M input + $15/M output) | `APPLY_MAX_COST_PER_JOB=0` to disable |
| **Missing file detection** (4.7) | Extends preflight: existence + non-zero size + `%PDF-` magic header + path-safety check | Built-in |
| **Dynamic blacklist** (4.10) | Auto-skips sites with > 85 % failure rate AND ≥ 3-streak in the last 30 days (configurable) | `APPLY_ENABLE_BLACKLIST=1` to turn on; `APPLY_BLACKLIST_FAILURE_THRESHOLD`, `APPLY_BLACKLIST_STREAK_THRESHOLD`, `APPLY_BLACKLIST_DAYS`, `APPLY_BLACKLIST_MIN_ATTEMPTS` to tune |
| **MCP fallback** (4.5) | If the local Ollama agent loop is unavailable, falls back to a Playwright MCP server if installed | Built-in detection |
| **Per-site form schema cache** (4.8) | Once a site's form structure is learned, it's cached and re-used on subsequent applies (saves 2-3 turns of discovery) | Built-in |

### 6.1 Telegram approval flow

When a job reaches the `pending_approval` state, ApplyPilot sends you a Telegram message with a **2-row inline keyboard**:

```
⏳ Approval needed: Senior AI Engineer
   🏢 Lumiform
   🌐 linkedin
   📊 Fit score: 9/10

Tap a button to preview, approve or decline.

[📄 Resume]  [✉️ Cover]
[✅ Approve] [❌ Decline]
```

**Row 1** is for previewing the tailored resume and cover letter without leaving Telegram — the bot will reply with the file content (text files are sent inline, PDFs are sent as documents plus a short text summary). **Row 2** is the final decision. Both rows stay tappable, so you can preview-then-approve in any order.

**How it works:**

1. `applypilot.apply.notifier.notify_approval_needed(job)` is called when a job enters `pending_approval`
2. The notifier sends a Telegram message with a 2-row `reply_markup.inline_keyboard` (4 buttons total)
3. **You tap** any of the 4 buttons in Telegram
4. The polling daemon (`scripts/telegram_callback_daemon.py`) catches the `callback_query` event
5. For `view_resume` / `view_cover` callbacks: daemon looks up the file path in the DB, reads the tailored file (with pypdf fallback for PDFs, capped at 5 MB), and sends it back as a new Telegram message
6. For `approve` / `decline` callbacks: daemon calls `mark_approval_approved()` (idempotent: only transitions from `pending_approval` → `approved`, sets `approved_at=now`) or `mark_approval_declined()` in the DB
7. Daemon sends `answerCallbackQuery` to clear the "loading" spinner
8. For approve/decline only: daemon edits the original message to show the result (e.g. `✅ Approved by @yourusername`). For preview callbacks, the original message is left intact so you can keep tapping buttons.

**Architecture:**

```
┌─────────────────────┐
│ applypilot pipeline │
│ (notifier.py)       │  ── sendMessage with inline_keyboard ──>  Telegram
└─────────────────────┘
                                                            ▲
                                                            │ callback_query
┌─────────────────────┐                                     │
│ scripts/            │  ──── getUpdates (long-poll) ────────┘
│ telegram_callback_  │  ──── mark_approval_approved ────>  DB
│ daemon.py           │  ──── answerCallbackQuery ───────>  Telegram
│ (background)        │  ──── editMessageText ────────────>  Telegram
└─────────────────────┘
```

**Manage the daemon:**

```bash
# Start (in background, detached from shell)
python3 -m applypilot telegram-listener start

# Check if it's running
python3 -m applypilot telegram-listener status

# Stop it
python3 -m applypilot telegram-listener stop

# Restart (e.g. after editing the script)
python3 -m applypilot telegram-listener restart
```

The daemon writes its PID to `/tmp/applypilot_telegram_listener.pid` and logs to `/tmp/applypilot_telegram_listener.log`. Env var overrides:
- `APPLY_TELEGRAM_POLL_INTERVAL` (default `1.5` seconds)
- `APPLY_TELEGRAM_LONG_POLL` (default `1` = enabled; long-polling reduces API calls)
- `APPLY_TELEGRAM_LONG_POLL_TIMEOUT` (default `25` seconds)
- `APPLY_TELEGRAM_LISTENER_LOG` (default `/tmp/applypilot_telegram_listener.log`)
- `APPLY_TELEGRAM_LISTENER_PID` (default `/tmp/applypilot_telegram_listener.pid`)
- `APPLY_TELEGRAM_EDIT_AFTER_CALLBACK` (default `1` = edit message after decision)

**Run as a cron** (so the daemon survives reboots and is always listening):

```bash
# Add to ~/.hermes/scripts/ or your existing cron runner
python3 -m applypilot telegram-listener restart
```

**How URLs are encoded in buttons:** Telegram buttons can carry at most **64 bytes** of `callback_data`. The notifier handles long URLs by SHA-256-hashing them and storing the mapping in `~/.applypilot/telegram_hash_registry.json`. The daemon reads this registry to resolve hash back to URL.

The output format is uniform across all 4 button types:

| Form | Format | When |
|---|---|---|
| Raw | `<prefix>:<url>` (e.g. `approve:https://…`) | URL ≤ 64 − len(prefix) − 1 bytes |
| Hashed | `<prefix>:h:<12-hex>` (e.g. `view_resume:h:9a7ae30340af`) | URL is too long for raw form |

The resolver calls `partition(':')` to split prefix from payload, then checks if the payload starts with `h:`. This format is the same for approve / decline / view_resume / view_cover, so the daemon's switch logic is one-liner.

**Security:** The daemon only processes `callback_query` events from your own `chat_id` (from env). Other users tapping the buttons are ignored with an "Unauthorized chat" answer.

**Testing without Telegram:** You can still approve/decline from the Streamlit Dashboard — it calls the same `mark_approval_approved()` / `mark_approval_declined()` functions, just with `actor='dashboard'` instead of `actor='telegram'`. The approval is recorded identically; the audit log only differs in the `actor` string.

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

`store_jobs()` has **three layers of deduplication** so the same job never lands twice:

1. **URL canonicalization** — `_canonicalize_url()` strips tracking query params before storage. Stripped: `utm_*`, `trk`, `ref`, `vjk`, `fromage`, `gclid`, `fbclid`, `_ga`, `_gl`, `mc_cid`, `mc_eid`, `msclkid`, `vero_id`, `vero_conv`, `from`, etc. Essential params are kept: Indeed `jk`, LinkedIn `currentJobId`, `q`, `pageNum`. Trailing slashes are normalised. This collapses LinkedIn `?trk=…` and Indeed `?vjk=…` variants of the same job into a single canonical URL.
2. **In-batch dedup** — `store_jobs()` keeps a `seen_in_batch: set[str]` for the current call. If a single JobSpy crawl returns the same job twice (e.g. overlapping query results), the second one is counted as a duplicate without a DB round-trip.
3. **DB-level PRIMARY KEY** — `jobs.url` is the SQLite PRIMARY KEY. Even if both layers above are bypassed (e.g. race condition between two parallel discover workers), an `INSERT` on a duplicate URL raises `IntegrityError`, which `store_jobs()` catches and counts as a duplicate.

Verified end-to-end with a synthetic 6-job batch (3 distinct canonical URLs + 3 tracking-param variants): the first insert added 3 rows, the second insert added 0 rows but counted all 6 as duplicates. After the test, 0 exact-URL and 0 canonical-URL duplicates remained in the live DB.

### Watchdog (orphan-process prevention)

The 4-hourly discover cron uses `flock -n` on `/tmp/applypilot_discover.lock` to prevent stacking. If a previous discover is still running, the new tick exits immediately (logs "Another discover is already running — skipping"). The lock auto-releases when the process exits (no stale lockfiles).

### Per-site analytics (4.9) & dynamic blacklist (4.10)

ApplyPilot tracks apply success rate per site and uses it to auto-skip broken ones.

```bash
# Show all sites with their success rate (last 30 days, min 3 attempts)
python3 -m applypilot sites

# Only show blacklisted sites
python3 -m applypilot sites --blacklist

# Adjust the look-back window / min attempts / thresholds
python3 -m applypilot sites --days 60 --min-attempts 5
python3 -m applypilot sites --blacklist --days 7

# JSON output for piping
python3 -m applypilot sites --json | jq
```

**A site is blacklisted when BOTH conditions hold** (defaults shown):
- failure_rate > 0.85 in the last 30 days
- recent_failure_streak ≥ 3 consecutive failures

**Tunable env vars** (read on every call, no restart):
- `APPLY_BLACKLIST_FAILURE_THRESHOLD` (default `0.85`)
- `APPLY_BLACKLIST_STREAK_THRESHOLD` (default `3`)
- `APPLY_BLACKLIST_DAYS` (default `30`)
- `APPLY_BLACKLIST_MIN_ATTEMPTS` (default `3`)

**To turn on auto-skip in preflight:**
```bash
export APPLY_ENABLE_BLACKLIST=1   # add to ~/.applypilot/.env for permanent
```

Programmatic access (for scripts / UI):
```python
from applypilot.database import (
    get_site_stats, get_dynamic_blacklist,
    is_site_blacklisted, get_blacklist_as_dict,
)
# returns: [{site, attempts, applied, failed, expired, captcha, login_issue,
#            dry_run, success_rate, failure_rate, recent_failure_streak}]
stats = get_site_stats(days=30, min_attempts=3)
bl = get_dynamic_blacklist()  # subset of stats
blacklisted, reason = is_site_blacklisted("linkedin")
```

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

### Pipeline operates on a phantom database (Hermes $HOME pitfall)

**Symptom:** Operations like `applypilot purge`, `applypilot sites`, or `applypilot status` show 0 jobs even though `~/.applypilot/applypilot.db` has thousands. You may also see `OperationalError: no such column: approved_at` because the phantom DB is missing recent columns.

**Cause:** Under Hermes profile isolation, `$HOME` is set to a sandboxed path like `/home/bostjan/.hermes/profiles/osebno/home/`. `applypilot.config` falls back to `Path.home() / ".applypilot"`, which is a *different* directory from your real `~/.applypilot/`. The CLI creates a brand-new empty DB there on first connect, and silently operates on it from then on.

**Fix (built into applypilot v0.3.0+):** `applypilot/config.py` now uses a `_resolve_app_dir()` helper that scans several candidate paths and picks the first one that contains `applypilot.db`. The same fix is in the Telegram daemon's `_resolve_app_dir()`. So both should resolve to the real `/home/bostjan/.applypilot/` automatically.

**Workaround if you hit it:** set `APPLYPILOT_DIR` (or `APPLY_APPDIR`) to the real path:

```bash
export APPLYPILOT_DIR=/home/bostjan/.applypilot
python3 -m applypilot purge --older-than-days 2 --dry-run
```

**Verify the resolver is working:**

```python
python3 -c "import applypilot.config; print(applypilot.config.APP_DIR)"
# Should print: /home/bostjan/.applypilot
# NOT: /home/bostjan/.hermes/profiles/osebno/home/.applypilot
```

If it prints the sandboxed path, your DB isn't being found by the resolver — check that the DB file exists and that you have read permission.

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
2. Telegram notification sent to `@sluzhbot` (with inline-keyboard buttons) when a job is queued
3. You either tap the button in Telegram or click **Approve** in the Streamlit Dashboard — browser opens with form pre-filled
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