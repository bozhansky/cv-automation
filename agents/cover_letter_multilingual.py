"""
Custom cover letter generation with Slovenian language support.
Extends the default applypilot cover letter generator.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import COVER_LETTER_DIR, RESUME_PATH, load_profile
from applypilot.database import get_connection
from applypilot.llm import get_client

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5

# Slovenian cover letter prompt template
SLOVENIAN_COVER_PROMPT = """Napiši spremno pismo za {sign_off_name}. Cilj je pridobiti razgovor.

STRUKTURA: 3 kratka odstavka. Pod 250 besedami. Vsaka poved mora imeti vrednost.

ODSTAVEK 1 (2-3 povedi): Začni s konkretno rešitvijo, ki si jo TI razvil in rešuje NJIHOV problem. Ne "Zanimate me za to delovno mesto." Ne "To delo se ujema z mojimi izkušnjami." Začni z delom.

ODSTAVEK 2 (3-4 povedi): Izberi 2 dosežka iz življenjepisa, ki sta NAJbolj relevantna za TO delo. Uporabi številke. Prikazuj kot reševanje njihovega problema, ne kot seznam svojih dosežkov.

ODSTAVEK 3 (1-2 povedi): Ene konkretne stvari o podjetju iz oglasa (produkt, tehnični izziv, struktura ekipe). Nato zaključi z "Lep pozdrav" ali "S spoštovanjem". Nič drugega.

TON:
- Piši kot pravi inženir, ki piše nekemu, ki ga spoštuje. Ne formalno, ne preveč sproščeno. Naravnost.
- NIKOLI ne razlagaj, kaj počneš. SLABO: "To prikazuje mojo predanost X." DOBRO: Povej dejstvo in pojdi naprej.
- NIKOLI ne uporabljaj odmikajočih se izrazov. SLABO: "bi lahko rešilo nekatere vaše izzive." DOBRO: "rešuje isti problem, s katerim se vaša ekipa sooča."
- Vsaka poved mora vsebovati številko, ime orodja ali konkreten rezultat. Če ne, izbriši.
- Preberi na glas. Če zveni kot da je napisal robot, prepiši.

Podpiši se samo z: "{sign_off_name}"

Izpiši SAMO besedilo pisma. Brez zadeve. Brez "Tukaj je spremno pismo:" predgovora. Brez opomb po podpisu.
Začni NEPOSREDNO s "Spoštovani," in končaj z imenom."""


def _build_english_cover_prompt(profile: dict) -> str:
    """Build the standard English cover letter prompt."""
    personal = profile.get("personal", {})
    sign_off_name = personal.get("preferred_name") or personal.get("full_name", "")
    
    return f"""Write a cover letter for {sign_off_name}. The goal is to get an interview.

STRUCTURE: 3 short paragraphs. Under 250 words. Every sentence must earn its place.

PARAGRAPH 1 (2-3 sentences): Open with a specific thing YOU built that solves THEIR problem. Not "I'm excited about this role." Not "This role aligns with my experience." Start with the work.

PARAGRAPH 2 (3-4 sentences): Pick 2 achievements from the resume that are MOST relevant to THIS job. Use numbers. Frame as solving their problem, not listing your accomplishments.

PARAGRAPH 3 (1-2 sentences): One specific thing about the company from the job description (a product, a technical challenge, a team structure). Then close. "Happy to walk through any of this in more detail." or "Let's discuss." Nothing else.

VOICE:
- Write like a real engineer emailing someone they respect. Not formal, not casual. Just direct.
- NEVER narrate or explain what you're doing. BAD: "This demonstrates my commitment to X." GOOD: Just state the fact and move on.
- NEVER hedge. BAD: "might address some of your challenges." GOOD: "solves the same problem your team is facing."
- Every sentence should contain either a number, a tool name, or a specific outcome. If it doesn't, cut it.
- Read it out loud. If it sounds like a robot wrote it, rewrite it.

Sign off: just "{sign_off_name}"

Output ONLY the letter text. No subject lines. No "Here is the cover letter:" preamble. No notes after the sign-off.
Start DIRECTLY with "Dear Hiring Manager," and end with the name."""


def is_slovenian_job(job: dict) -> bool:
    """Detect if job requires Slovenian language based on source or metadata."""
    site = (job.get("site") or "").lower()
    url = (job.get("url") or "").lower()
    location = (job.get("location") or "").lower()
    
    # Check source site
    if "mojedelo" in site or "mojedelo" in url:
        return True
    
    # Check for Slovenian location indicators
    slovenian_locations = ["slovenia", "slovenija", "ljubljana", "maribor", 
                          "koper", "celje", "novo mesto", "izola"]
    for loc in slovenian_locations:
        if loc in location:
            return True
    
    # Check for language flag in job metadata
    if job.get("language") == "sl" or job.get("cover_letter_language") == "slovenian":
        return True
    
    return False


def generate_cover_letter_multilingual(
    resume_text: str,
    job: dict,
    profile: dict,
    max_retries: int = 3,
    validation_mode: str = "normal",
) -> tuple[str, str]:
    """Generate cover letter, auto-detecting language.
    
    Returns:
        Tuple of (letter_text, language_code)
    """
    is_slovenian = is_slovenian_job(job)
    language = "sl" if is_slovenian else "en"
    
    job_text = (
        f"TITLE: {job['title']}\\n"
        f"COMPANY: {job['site']}\\n"
        f"LOCATION: {job.get('location', 'N/A')}\\n\\n"
        f"DESCRIPTION:\\n{(job.get('full_description') or '')[:6000]}"
    )
    
    client = get_client()
    
    # Build appropriate prompt
    if is_slovenian:
        personal = profile.get("personal", {})
        sign_off_name = personal.get("preferred_name") or personal.get("full_name", "")
        prompt = SLOVENIAN_COVER_PROMPT.format(sign_off_name=sign_off_name)
        greeting = "Spoštovani,"
    else:
        prompt = _build_english_cover_prompt(profile)
        greeting = "Dear Hiring Manager,"
    
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": (
            f"RESUME:\\n{resume_text}\\n\\n---\\n\\n"
            f"TARGET JOB:\\n{job_text}\\n\\n"
            f"Write the cover letter in {'Slovenian' if is_slovenian else 'English'}:"
        )},
    ]
    
    letter = client.chat(messages, max_tokens=1024, temperature=0.7)
    
    # Clean up - ensure proper greeting
    letter = letter.strip()
    if is_slovenian and not letter.startswith("Spoštovani"):
        # Replace English greeting if present
        letter = re.sub(r'^(Dear\s+\w+,|Dear\s+Hiring Manager,)', 'Spoštovani,', letter, flags=re.IGNORECASE)
    
    return letter, language


def run_cover_letters_multilingual(min_score: int = 7, limit: int = 20) -> dict:
    """Generate cover letters with automatic language detection.
    
    Slovenian jobs (mojedelo.com, Slovenian locations) get Slovenian cover letters.
    All others get English cover letters.
    """
    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()
    
    # Fetch jobs needing cover letters
    jobs = conn.execute(
        "SELECT * FROM jobs "
        "WHERE fit_score >= ? AND tailored_resume_path IS NOT NULL "
        "AND full_description IS NOT NULL "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        "AND COALESCE(cover_attempts, 0) < ? "
        "ORDER BY fit_score DESC LIMIT ?",
        (min_score, MAX_ATTEMPTS, limit),
    ).fetchall()
    
    if not jobs:
        log.info("No jobs needing cover letters (score >= %d).", min_score)
        return {"generated": 0, "errors": 0, "elapsed": 0.0, "languages": {}}
    
    # Convert rows to dicts
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]
    
    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Generating cover letters for %d jobs...", len(jobs))
    
    t0 = time.time()
    completed = 0
    saved = 0
    error_count = 0
    language_counts = {"en": 0, "sl": 0}
    
    for job in jobs:
        completed += 1
        try:
            letter, language = generate_cover_letter_multilingual(
                resume_text, job, profile
            )
            language_counts[language] = language_counts.get(language, 0) + 1
            
            # Build filename with language indicator
            safe_title = re.sub(r"[^\\w\\s-]", "", job["title"])[:50].strip().replace(" ", "_")
            safe_site = re.sub(r"[^\\w\\s-]", "", job["site"])[:20].strip().replace(" ", "_")
            lang_suffix = "_SL" if language == "sl" else "_EN"
            prefix = f"{safe_site}_{safe_title}{lang_suffix}"
            
            cl_path = COVER_LETTER_DIR / f"{prefix}_CL.txt"
            cl_path.write_text(letter, encoding="utf-8")
            
            # Update DB
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, "
                "cover_attempts=COALESCE(cover_attempts,0)+1, language=? WHERE url=?",
                (str(cl_path), now, language, job["url"]),
            )
            conn.commit()
            saved += 1
            
            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            log.info(
                "%d/%d [OK] [%s] | %.1f jobs/min | %s",
                completed, len(jobs), language.upper(), rate * 60, job["title"][:40],
            )
            
        except Exception as e:
            error_count += 1
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), job["title"][:40], e)
            # Still increment attempt counter
            conn.execute(
                "UPDATE jobs SET cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (job["url"],),
            )
            conn.commit()
    
    elapsed = time.time() - t0
    log.info("Cover letters done: %d generated (%d EN, %d SL), %d errors", 
              saved, language_counts.get("en", 0), language_counts.get("sl", 0), error_count)
    
    return {
        "generated": saved,
        "errors": error_count,
        "elapsed": elapsed,
        "languages": language_counts,
    }


if __name__ == "__main__":
    # Run standalone
    result = run_cover_letters_multilingual()
    print(json.dumps(result, indent=2))