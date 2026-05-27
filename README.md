# ApplyPilot — Boštjan's Job Application Automation

This document explains how to use the ApplyPilot system to automate job discovery, AI-powered scoring, resume tailoring, cover letter generation, and auto-apply.

---

## Table of Contents

1. [How the Pipeline Works](#1-how-the-pipeline-works)
2. [Adding Job Search Resources](#2-adding-job-search-resources)
3. [Configuration Files](#3-configuration-files)
4. [Running the Pipeline](#4-running-the-pipeline)
5. [Auto-Apply Agent](#5-auto-apply-agent)
6. [Troubleshooting](#6-troubleshooting)

---

## 1. How the Pipeline Works

ApplyPilot runs in six sequential stages. Each stage reads from and writes to a local SQLite database (`~/.applypilot/applypilot.db`).

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
│  STAGE 2: enrich                                                      │
│  Visits each job URL to extract the full job description, salary,   │
│  requirements, and the direct application link.                     │
│  Output: Full text descriptions for all discovered jobs              │
│  Storage: applypilot.db "jobs" table (updated in place)              │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────────┐
│  STAGE 3: score                                                       │
│  Sends each job + your resume text to the LLM (Ollama) for scoring.  │
│  Assigns a fit score 1-10 based on role alignment, skills match,      │
│  location fit, and experience level.                                  │
│  Output: fit_score + reasoning for every job                         │
│  Storage: applypilot.db "jobs" table (fit_score, score_reasoning)    │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────────┐
│  STAGE 4: tailor                                                      │
│  For jobs scoring ≥ 7: rewrites your resume bullets to match the     │
│  job description keywords and phrasing.                              │
│  Validation: checks for banned words, alignment with your profile.   │
│  Output: tailored_resume/ directory with .txt files                  │
│  Storage: applypilot.db (tailored_resume_path per job)               │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────────┐
│  STAGE 5: cover                                                       │
│  Generates a personalised cover letter for each tailored job.       │
│  Each letter references specific requirements from the job posting.   │
│  Output: cover_letters/ directory with .txt files                    │
│  Storage: applypilot.db (cover_letter_path per job)                  │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────────┐
│  STAGE 6: pdf                                                         │
│  Converts all tailored resumes and cover letters to PDF format.      │
│  Output: tailored_resume/*.pdf and cover_letters/*.pdf               │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────────┐
│  STAGE 7: apply (custom Ollama agent — not yet built)                │
│  Opens each job application URL in Playwright-controlled Chrome.     │
│  Fills the form, uploads tailored resume + cover letter, answers    │
│  screening questions, and submits. Powered by Ollama LLM reasoning.  │
│  Status: applypilot.db updated (applied_at, apply_status)            │
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
    - "Germany"       # ← new
    - "Netherlands"   # ← new
    - "Sweden"        # ← new
    - "Anywhere"
  reject_patterns:
    - "onsite only"
    - "India"
    - "Philippines"
```

---

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

---

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
LLM_API_KEY=ollama                     # Any string (Ollama doesn't need real keys)
```

**Available Ollama models** (use `ollama list` to see yours):
- `gemma4:31b-cloud` — recommended, 31B params via Ollama Cloud
- `deepseek-v4-flash:cloud` — fast, good for structured tasks
- `qwen3.5:cloud` — strong reasoning
- `kimi-k2.6:cloud`
- `minimax-m2.7:cloud`

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

# Stage 2: enrich job descriptions
python3 -m applypilot run enrich

# Stage 3: score all jobs (requires LLM)
python3 -m applypilot run score

# Stage 4: tailor resumes for jobs scoring ≥7
python3 -m applypilot run tailor

# Stage 5: generate cover letters for tailored jobs
python3 -m applypilot run cover

# Stage 6: convert to PDF
python3 -m applypilot run pdf

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
| `--stream` | Run stages concurrently (faster but less verbose) |

### Checking status

```bash
export HOME=/home/bostjan
python3 -m applypilot status       # show DB counts
python3 -m applypilot dashboard    # open web dashboard
```

### Re-scoring with different settings

If you change the minimum score threshold, re-run tailor/cover only for qualifying jobs. ApplyPilot skips jobs that already have tailored resumes unless you force it.

---

## 5. Auto-Apply Agent

ApplyPilot's native auto-apply uses **Claude Code** to control Playwright MCP. This requires:
- `claude` CLI installed (`brew install anthropic/claude/claude`)
- `npx playwright install chromium`
- Anthropic API key

**For Ollama-powered auto-apply** (what you're building):

The idea is to replace the Claude Code call in `apply/launcher.py` with a direct Ollama chat completions call that reasons through the application form, then uses Playwright's Python API (`from playwright.sync_api import sync_playwright`) to execute the browser actions.

**Core tools to use from Playwright MCP** (mirror what ApplyPilot uses):

```
mcp__chrome__navigate(url)         → page.goto(url)
mcp__chrome__click(selector)       → page.click(selector)
mcp__chrome__fill(selector, text) → page.fill(selector, text)
mcp__chrome__press(key)           → page.press(selector, key)
mcp__chrome__select_option(...)   → page.select_option(...)
mcp__chrome__upload_file(...)      → page.set_input_files(...)
mcp__chrome__submit()              → page.click("[type=submit]")
mcp__chrome__screenshot()         → page.screenshot()
```

**Steps to build the Ollama apply agent:**

1. In `~/.applypilot/`, create `apply_agent/ollama_agent.py`
2. Use `playwright.sync_api` to launch a Chrome browser in headless mode
3. Call `OLLAMA_URL/v1/chat/completions` with `gemma4:31b-cloud` and the ApplyPilot apply prompt (from `apply/prompt.py`)
4. Parse the LLM's response to extract the next browser action (click/fill/navigate/etc.)
5. Execute the action via Playwright
6. Loop until form is submitted or the LLM says "done"
7. Screenshot the result and log apply status to `applypilot.db`

---

## 6. Troubleshooting

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