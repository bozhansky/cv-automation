"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


def _parse_since(value: Optional[str]) -> Optional[str]:
    """Resolve --since value to an ISO-8601 UTC string.

    Accepts:
      - None / "" -> None
      - ISO datetime ('2026-06-01T20:00:00', '2026-06-01 20:00:00') -> as-is
      - Relative shorthand: '24h', '12h', '7d', '30m' -> now() - that delta

    Returns:
        ISO-8601 string in UTC, or None if input is empty.
    """
    if not value:
        return None
    value = value.strip()
    # Relative shorthand: e.g. "24h", "7d", "30m", "2w"
    m = re.fullmatch(r"(\d+)\s*([mhdw])", value.lower())
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
        }[unit]
        return (datetime.now(timezone.utc) - delta).isoformat()
    # Assume ISO format; try a few common shapes
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    console.print(
        f"[red]Could not parse --since value '{value}'. "
        f"Use ISO datetime ('2026-06-01T20:00:00') or relative ('24h', '7d').[/red]"
    )
    raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(True, "--stream/--no-stream", help="Run stages concurrently (streaming mode). Default: enabled (~3-4x faster)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    since: Optional[str] = typer.Option(
        None,
        "--since",
        help=(
            "ISO datetime or relative ('24h', '7d'). Tailor/cover stages only "
            "process jobs discovered at/after this time. Example: '2026-06-01T20:00:00' "
            "or '24h' for the last day. Use this for daily cron runs."
        ),
    ),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
        since=_parse_since(since),
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("ollama-default", "--model", "-m", help="LLM model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/agent needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Legacy CLI agent + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  legacy-cli --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def purge(
    older_than_days: int = typer.Option(
        7,
        "--older-than-days",
        "-d",
        help="Delete jobs discovered more than this many days ago.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview what would be deleted without actually deleting anything.",
    ),
    include_applied: bool = typer.Option(
        False,
        "--include-applied",
        help="Also delete jobs you've already applied to. Off by default.",
    ),
    include_approved: bool = typer.Option(
        False,
        "--include-approved",
        help="Also delete jobs you've explicitly approved. Off by default.",
    ),
) -> None:
    """Delete old jobs and their tailored files (default: 7 days)."""
    from applypilot.database import purge_old_jobs

    console.print(
        f"\n[bold]ApplyPilot purge[/bold]  older_than_days={older_than_days}  "
        f"dry_run={dry_run}  include_applied={include_applied}  "
        f"include_approved={include_approved}\n"
    )
    result = purge_old_jobs(
        older_than_days=older_than_days,
        dry_run=dry_run,
        preserve_applied=not include_applied,
        preserve_approved=not include_approved,
    )
    if result.get("dry_run"):
        rows = result.get("rows_would_delete", 0)
        files = result.get("files_would_delete", 0)
        pa = result.get("preserved_applied", 0)
        pr = result.get("preserved_approved", 0)
        if rows == 0 and files == 0:
            console.print(
                f"  [green]Nothing to purge[/green] — no jobs older than {older_than_days} days. "
                f"({pa} applied, {pr} approved would be preserved.)"
            )
        else:
            console.print(
                f"  [yellow]DRY RUN[/yellow] — would delete "
                f"{rows} DB rows and "
                f"{files} files. "
                f"({pa} applied, {pr} approved would be preserved.)"
            )
    else:
        if result['purged'] == 0 and result['files_deleted'] == 0:
            console.print(
                f"  [green]Nothing to purge[/green] — no jobs older than {older_than_days} days. "
                f"({result['preserved_applied']} applied, "
                f"{result['preserved_approved']} approved preserved.)"
            )
        else:
            console.print(
                f"  [green]Purged:[/green] {result['purged']} jobs, "
                f"{result['files_deleted']} files deleted "
                f"({result['files_missing']} already missing). "
                f"[green]Preserved:[/green] "
                f"{result['preserved_applied']} applied, "
                f"{result['preserved_approved']} approved."
            )


@app.command()
def tailor(
    url: str = typer.Argument(..., help="Job URL to (re)tailor for."),
    min_score: int = typer.Option(0, "--min-score", help="Override the score check; default 0 (process any score)."),
) -> None:
    """On-demand tailoring for a single job (does not require score>=7)."""
    from applypilot.database import get_connection
    from applypilot.scoring.tailor import tailor_resume
    from applypilot.scoring.pdf import convert_to_pdf
    from applypilot.config import RESUME_PATH, TAILORED_DIR
    from applypilot.config import load_profile

    conn = get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    if not row:
        console.print(f"[red]Job not found:[/red] {url}")
        raise typer.Exit(code=1)
    job = dict(row)
    if job.get("fit_score") is not None and job["fit_score"] < min_score:
        console.print(
            f"[yellow]Skipping[/yellow] — job score {job['fit_score']} < {min_score}."
        )
        raise typer.Exit(code=1)

    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    console.print(f"Tailoring for: [bold]{job.get('title', '?')}[/bold]")
    tailored, report = tailor_resume(resume_text, job, profile, validation_mode="normal")
    base = _job_basename(job)
    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    txt_path = TAILORED_DIR / f"{base}.txt"
    txt_path.write_text(tailored, encoding="utf-8")
    try:
        pdf_path = str(convert_to_pdf(txt_path))
    except Exception as exc:
        log.warning("PDF render failed: %s", exc)
        pdf_path = str(txt_path)
    conn.execute(
        "UPDATE jobs SET tailored_resume_path = ?, tailored_at = ? WHERE url = ?",
        (pdf_path, datetime.now(timezone.utc).isoformat(), url),
    )
    conn.commit()
    console.print(f"[green]Saved:[/green] {pdf_path}")


@app.command()
def cover(
    url: str = typer.Argument(..., help="Job URL to (re)generate cover letter for."),
) -> None:
    """On-demand cover letter generation for a single job."""
    from applypilot.database import get_connection
    from applypilot.scoring.cover_letter import generate_cover_letter
    from applypilot.scoring.pdf import convert_to_pdf
    from applypilot.config import RESUME_PATH, COVER_LETTER_DIR
    from applypilot.config import load_profile

    conn = get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    if not row:
        console.print(f"[red]Job not found:[/red] {url}")
        raise typer.Exit(code=1)
    job = dict(row)
    if not job.get("tailored_resume_path"):
        console.print("[yellow]No tailored resume found; tailor first.[/yellow]")
        raise typer.Exit(code=1)

    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    console.print(f"Cover letter for: [bold]{job.get('title', '?')}[/bold]")
    letter = generate_cover_letter(resume_text, job, profile, validation_mode="normal")
    base = _job_basename(job)
    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    txt_path = COVER_LETTER_DIR / f"{base}_cover.txt"
    txt_path.write_text(letter, encoding="utf-8")
    try:
        pdf_path = str(convert_to_pdf(txt_path))
    except Exception as exc:
        log.warning("PDF render failed: %s", exc)
        pdf_path = str(txt_path)
    conn.execute(
        "UPDATE jobs SET cover_letter_path = ?, cover_letter_at = ? WHERE url = ?",
        (pdf_path, datetime.now(timezone.utc).isoformat(), url),
    )
    conn.commit()
    console.print(f"[green]Saved:[/green] {pdf_path}")


@app.command()
def packet(
    url: str = typer.Argument(..., help="Job URL to build a combined application packet PDF for."),
) -> None:
    """Build a combined resume+cover PDF for one job (ready to attach in one upload)."""
    from applypilot.database import get_connection
    from applypilot.scoring.pdf import combine_packet_pdf
    from applypilot.config import TAILORED_DIR, COVER_LETTER_DIR

    conn = get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    if not row:
        console.print(f"[red]Job not found:[/red] {url}")
        raise typer.Exit(code=1)
    job = dict(row)
    base = _job_basename(job)
    resume_pdf = TAILORED_DIR / f"{base}.pdf"
    cover_pdf = COVER_LETTER_DIR / f"{base}_cover.pdf"
    if not resume_pdf.exists():
        console.print(f"[red]No tailored resume PDF:[/red] {resume_pdf}")
        raise typer.Exit(code=1)
    if not cover_pdf.exists():
        console.print(f"[red]No cover letter PDF:[/red] {cover_pdf}")
        raise typer.Exit(code=1)
    packet_pdf = TAILORED_DIR / f"{base}_PACKET.pdf"
    combine_packet_pdf(resume_pdf, cover_pdf, packet_pdf)
    console.print(f"[green]Saved:[/green] {packet_pdf}")


def _job_basename(job: dict) -> str:
    """Generate a filesystem-safe basename for a job (site + title slug)."""
    import re as _re
    site = (job.get("site") or "job").lower()
    title = job.get("title") or "untitled"
    slug = _re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:50]
    return f"{site}_{slug}"


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.applypilot/.env (run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Legacy CLI agent
    legacy_bin = shutil.which("legacy-cli")
    if legacy_bin:
        results.append(("Legacy CLI agent", ok_mark, legacy_bin))
    else:
        results.append(("Legacy CLI agent", fail_mark,
                        "Install from https://ollama (see https://ollama.com) (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Legacy CLI agent + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Legacy CLI agent + Chrome + Node.js)[/dim]")

    console.print()


# -------------------------------------------------------------------
# telegram-listener: start/stop/status for the Telegram callback daemon
# -------------------------------------------------------------------
@app.command()
def telegram_listener(
    action: str = typer.Argument(..., help="start | stop | status | restart"),
) -> None:
    """Manage the Telegram callback polling daemon (4.6 inline-keyboard approval flow).

    The daemon listens for button taps on Telegram messages sent by
    `notify_approval_needed()`. When you tap ✅ Approve or ❌ Decline on a
    Telegram notification, this daemon catches the callback_query, calls
    `mark_approval_approved()` or `mark_approval_declined()` in the DB,
    and edits the original message to show the result.

    Examples:
        python3 -m applypilot telegram-listener start
        python3 -m applypilot telegram-listener status
        python3 -m applypilot telegram-listener stop
        python3 -m applypilot telegram-listener restart
    """
    import os
    import signal
    import subprocess
    from pathlib import Path as _P

    pid_path = _P(os.environ.get(
        "APPLY_TELEGRAM_LISTENER_PID",
        "/tmp/applypilot_telegram_listener.pid",
    ))
    # Locate the daemon script: the applypilot package ships its own copy at
    # <pkg>/scripts/telegram_callback_daemon.py, OR the user's local copy at
    # <cwd>/scripts/telegram_callback_daemon.py. Prefer the local one.
    here = _P(__file__).parent
    candidates = [
        _P.cwd() / "scripts" / "telegram_callback_daemon.py",
        here / "scripts" / "telegram_callback_daemon.py",
    ]
    daemon_script = next((c for c in candidates if c.exists()), None)
    if daemon_script is None and action in ("start", "restart"):
        typer.echo("❌ Could not find telegram_callback_daemon.py. Looked in:")
        for c in candidates:
            typer.echo(f"   • {c}")
        raise typer.Exit(code=2)

    def _is_running() -> int | None:
        """Return the daemon's PID if running, else None."""
        if not pid_path.exists():
            return None
        try:
            pid = int(pid_path.read_text().strip())
        except (ValueError, OSError):
            return None
        # Check if the process is alive
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return None
        return pid

    def _start() -> None:
        existing = _is_running()
        if existing is not None:
            typer.echo(f"ℹ️  Daemon already running (pid={existing})")
            return
        typer.echo(f"Starting Telegram callback daemon: {daemon_script}")
        log_path = _P(os.environ.get(
            "APPLY_TELEGRAM_LISTENER_LOG",
            "/tmp/applypilot_telegram_listener.log",
        ))
        # Launch via setsid so the daemon survives our shell exit
        try:
            log_fd = open(log_path, "a")
        except OSError as e:
            typer.echo(f"❌ Cannot open log file {log_path}: {e}")
            raise typer.Exit(code=3)
        proc = subprocess.Popen(
            [sys.executable, str(daemon_script)],
            stdout=log_fd,
            stderr=log_fd,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach from parent process group
        )
        # Give the daemon a moment to write its PID file
        for _ in range(20):
            time.sleep(0.1)
            if pid_path.exists():
                break
        pid = _is_running()
        if pid is None:
            typer.echo(f"❌ Daemon failed to start. Last 20 log lines:")
            try:
                lines = log_path.read_text().splitlines()[-20:]
                for line in lines:
                    typer.echo(f"   {line}")
            except OSError:
                pass
            raise typer.Exit(code=4)
        typer.echo(f"✅ Started Telegram callback daemon (pid={pid})")
        typer.echo(f"   Log: {log_path}")
        typer.echo(f"   Stop with: python3 -m applypilot telegram-listener stop")

    def _stop() -> None:
        pid = _is_running()
        if pid is None:
            typer.echo("ℹ️  Daemon not running (no PID file or process is dead)")
            # Cleanup stale pid file
            try:
                pid_path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        typer.echo(f"Stopping Telegram callback daemon (pid={pid})...")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            typer.echo(f"❌ Failed to send SIGTERM: {e}")
            raise typer.Exit(code=5)
        # Wait up to 3s for graceful shutdown
        for _ in range(30):
            time.sleep(0.1)
            if _is_running() is None:
                break
        else:
            # Force kill
            typer.echo("Daemon did not stop gracefully; sending SIGKILL")
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        typer.echo("✅ Stopped.")

    def _status() -> None:
        pid = _is_running()
        if pid is None:
            typer.echo("⏹  Telegram callback daemon is NOT running")
            typer.echo(f"   (PID file: {pid_path})")
            return
        typer.echo(f"✅ Telegram callback daemon is running (pid={pid})")
        log_path = _P(os.environ.get(
            "APPLY_TELEGRAM_LISTENER_LOG",
            "/tmp/applypilot_telegram_listener.log",
        ))
        if log_path.exists():
            typer.echo(f"   Recent log lines:")
            try:
                lines = log_path.read_text().splitlines()[-5:]
                for line in lines:
                    typer.echo(f"     {line[:120]}")
            except OSError:
                pass

    if action == "start":
        _start()
    elif action == "stop":
        _stop()
    elif action == "status":
        _status()
    elif action == "restart":
        _stop()
        time.sleep(0.5)
        _start()
    else:
        typer.echo(f"❌ Unknown action: {action!r}. Use start/stop/status/restart.")
        raise typer.Exit(code=1)


# -------------------------------------------------------------------
# sites: per-site success-rate analytics + dynamic blacklist (4.9, 4.10)
# -------------------------------------------------------------------
@app.command()
def sites(
    days: int = typer.Option(30, "--days", help="Look at the last N days"),
    min_attempts: int = typer.Option(3, "--min-attempts", help="Min apply attempts to be included"),
    blacklist: bool = typer.Option(False, "--blacklist", help="Only show blacklisted sites"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON instead of a table"),
) -> None:
    """Per-site apply success-rate analytics (4.9) + dynamic blacklist (4.10)."""
    from applypilot.database import get_site_stats, get_dynamic_blacklist
    from rich.console import Console
    from rich.table import Table

    if blacklist:
        sites_data = get_dynamic_blacklist(days=days, min_attempts=min_attempts)
        title = f"Dynamic Blacklist (last {days} days, min {min_attempts} attempts)"
    else:
        sites_data = get_site_stats(days=days, min_attempts=min_attempts)
        title = f"Per-Site Success Rate (last {days} days, min {min_attempts} attempts)"

    if json_output:
        import json as _json
        typer.echo(_json.dumps(sites_data, indent=2))
        return

    console = Console()
    if not sites_data:
        console.print(f"[yellow]No sites match the criteria[/yellow]")
        return

    table = Table(title=title, show_header=True, header_style="bold magenta")
    table.add_column("Site", style="cyan")
    table.add_column("Attempts", justify="right")
    table.add_column("Applied", justify="right", style="green")
    table.add_column("Failed", justify="right", style="red")
    table.add_column("Success%", justify="right")
    table.add_column("Streak", justify="right")

    for s in sites_data:
        site = s["site"]
        attempts = s["attempts"]
        applied = s["applied"]
        failed = s["failed"] + s["expired"] + s["captcha"] + s["login_issue"]
        success = s["success_rate"] * 100
        streak = s["recent_failure_streak"]
        streak_str = f"[red]{streak}[/red]" if streak >= 3 else str(streak)
        success_str = f"[green]{success:.0f}%[/green]" if success >= 50 else f"[red]{success:.0f}%[/red]"
        table.add_row(site, str(attempts), str(applied), str(failed), success_str, streak_str)

    console.print(table)
    console.print()
    if not blacklist:
        bl = get_dynamic_blacklist(days=days, min_attempts=min_attempts)
        if bl:
            bl_names = ", ".join(s["site"] for s in bl)
            console.print(f"[bold red]⚠  {len(bl)} site(s) blacklisted: {bl_names}[/bold red]")
            console.print(f"[dim]  Set APPLY_ENABLE_BLACKLIST=1 to auto-skip them in preflight.[/dim]")
        else:
            console.print(f"[dim]No sites currently blacklisted.[/dim]")


if __name__ == "__main__":
    app()
