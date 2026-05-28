"""
Employer career-page scraper — supplements JobSpy with direct career site scraping.
Adds companies from employers.yaml to the ApplyPilot DB.
"""

import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from applypilot import config
from applypilot.database import get_connection, init_db, store_jobs

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(total=3, backoff_factor=1.0, status_forcelist=[429, 500, 502, 503, 504]),
        raise_on_status=False,
    ),
)


def _scrape_career_page(url: str, search_terms: list[str]) -> list[dict]:
    """Scrape a company career page for jobs matching search_terms."""
    jobs = []
    try:
        resp = SESSION.get(url, timeout=15)
        if resp.status_code != 200:
            log.warning("[%s] HTTP %d", url, resp.status_code)
            return jobs
        soup = BeautifulSoup(resp.text, "html.parser")
        text_all = soup.get_text().lower()
        title_hits = []
        for term in search_terms:
            term_l = term.lower()
            if term_l in text_all:
                title_hits.append(term)
        if not title_hits:
            return jobs
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if not text or len(text) < 4:
                continue
            text_lower = text.lower()
            if any(t.lower() in text_lower for t in search_terms):
                job_url = href if href.startswith("http") else urljoin(url, href)
                jobs.append({
                    "title": text[:200],
                    "url": job_url,
                    "company": None,
                    "location": "Remote / EU",
                    "description": None,
                    "site": url,
                })
    except Exception as e:
        log.warning("[%s] %s", url, e)
    return jobs


def _store_career_jobs(jobs: list[dict], source_label: str) -> tuple[int, int]:
    """Store career-page scraped jobs into DB."""
    now = datetime.now(timezone.utc).isoformat()
    new = existing = 0
    conn = get_connection()
    for job in jobs:
        url = job.get("url") or ""
        if not url or url == "nan":
            continue
        try:
            conn.execute(
                "INSERT OR IGNORE INTO jobs "
                "(url, title, company, location, site, strategy, discovered_at, detail_scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), job.get("company"), job.get("location"),
                 source_label, "career_page", now, None),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1
    conn.commit()
    return new, existing


def run_employer_discovery(cfg: dict | None = None) -> dict:
    """Scrape career pages for all employers in employers.yaml."""
    if cfg is None:
        cfg = config.load_employers_config()

    if not cfg or not cfg.get("employers"):
        log.info("No employers.yaml found — skipping career-page discovery")
        return {"new": 0, "existing": 0, "employers": 0}

    init_db()
    total_new = total_existing = 0
    employers_done = 0

    for employer in cfg["employers"]:
        name = employer.get("name", "unknown")
        careers_url = employer.get("careers_url", "")
        search_terms = employer.get("search_terms", [])
        if not careers_url or not search_terms:
            continue
        log.info("[%s] Scraping %s", name, careers_url)
        jobs = _scrape_career_page(careers_url, search_terms)
        n_new, n_existing = _store_career_jobs(jobs, name)
        total_new += n_new
        total_existing += n_existing
        employers_done += 1
        log.info("[%s] %d new, %d dupes", name, n_new, n_existing)
        time.sleep(2)  # polite delay between employers

    log.info(
        "Employer discovery done: %d new, %d dupes across %d employers",
        total_new, total_existing, employers_done,
    )
    return {"new": total_new, "existing": total_existing, "employers": employers_done}