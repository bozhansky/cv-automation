# applypilot Patches

This directory contains local modifications to the upstream
[`Pickle-Pixel/ApplyPilot`](https://github.com/Pickle-Pixel/ApplyPilot) pip
package (v0.3.0). The code in `applypilot/` is **installed via pip** at:

```
~/.hermes/profiles/osebno/home/.local/lib/python3.12/site-packages/applypilot/   # primary (Hermes shadow)
~/.local/lib/python3.12/site-packages/applypilot/                                  # secondary
```

We track our patches here in this repo so they can be:
1. **Reviewed** via git history
2. **Re-applied** on a fresh install (e.g. when upgrading or on a new machine)
3. **Pushed to a fork** of Pickle-Pixel/ApplyPilot if/when the user wants
   upstream to accept them

## What's patched

| File | Reason | What changed |
|---|---|---|
| `cli.py` | On-demand subcommands + sites analytics + telegram-listener CLI | Added `tailor`, `cover`, `packet`, `purge`, `sites` subcommands; threaded `--since` through `run`; flipped streaming default ON; added `--stream/--no-stream`; added `telegram_listener start\|stop\|status\|restart` (with `.env` loading for the daemon subprocess) |
| `config.py` | Hermes `$HOME` multi-path resolver | `_resolve_app_dir()` scans `/home/bostjan/.applypilot`, `Path.home()/.applypilot`, the Hermes profile sandbox, and `APPLYPILOT_DIR` env var, picking the first path that contains `applypilot.db`. Stops the CLI from creating a phantom empty DB under Hermes profile isolation. |
| `database.py` | Schema migrations + URL dedup + per-site analytics + dynamic blacklist | `ensure_columns()` for `approved_at`/`apply_cost_usd`; `ensure_indexes()`; `_canonicalize_url()`; `purge_old_jobs()`; `get_site_stats()`; `get_dynamic_blacklist()`; `is_site_blacklisted()`; `get_blacklist_as_dict()`; 3-layer dedup in `store_jobs()` (canonicalize + in-batch + DB PRIMARY KEY) |
| `apply/launcher.py` | Apply-stage safety rails | `preflight_check()` upgraded (existence + size + PDF header + path safety); `mark_result()` now fires `notify_applied`; cost stored to `apply_cost_usd`; 3-tuple return; dry-run marks `dry_run_ok`; `_run_job_ollama()` plumbs `dry_run`; `_run_job_mcp()` MCP fallback dispatcher |
| `apply/ollama_agent.py` | Submit gate (4.1) + cost cap (4.2) | Added `submit_application` tool with screenshot gate; `_screenshot_taken_this_turn` flag; per-turn cost cap; `dry_run` plumbing |
| `apply/prompt.py` | Better submit instructions + cached form schema injection | Updated `submit_instruction` for both dry-run and live paths to use the new gate; injects `{cached_schema_section}` for known sites |
| `apply/notifier.py` | NEW — Telegram notifications | `notify_applied()`, `notify_failed()`, `notify_approval_needed()` (with 2-row inline-keyboard `[📄 Resume]/[✉️ Cover]/[✅ Approve]/[❌ Decline]`), `answer_callback_query()`, threaded send, env-var-driven config (supports both `TELEGRAM_*` and `APPLY_TELEGRAM_*` env var names); SHA-256 URL-hash registry for long-URL button callbacks (uniform `<prefix>:<url>` / `<prefix>:h:<hash>` format across all 4 action types); 5 MB size cap on preview reads; multi-path secrets-file lookup for Hermes HOME isolation |
| `scripts/telegram_callback_daemon.py` | NEW — Telegram callback polling daemon (4.6) | Long-polls `getUpdates` for `callback_query` events from `notify_approval_needed()`; calls `mark_approval_approved` / `mark_approval_declined` (idempotent — only transitions from `pending_approval`); handles `view_resume` / `view_cover` actions by sending the tailored file (text inline, PDF as document + summary); manages its own PID + log files; signal-safe shutdown (SIGTERM/SIGINT) |
| `apply/mcp_fallback.py` | NEW — MCP Playwright fallback (4.5) | `is_mcp_available()`, `discover_mcp_server()`, `mcp_status()`, `get_mcp_tools()`; activated by `APPLY_AGENT=mcp` |
| `apply/form_schema_cache.py` | NEW — Per-site form schema cache (4.8) | `get_schema()`, `save_schema()`, `update_usage()`, `prune_stale()`, `get_schema_for_prompt()`; caches field selectors per site |

## Re-apply script

To re-apply all patches to a fresh applypilot install:

```bash
./patches/apply_patches.sh
```

The script:
1. Detects the applypilot install location (Hermes shadow first, then `~/.local`)
2. Backs up originals to `applypilot.bak.$(date +%Y%m%d_%H%M%S)/`
3. Copies each file in this directory to both install paths
4. Verifies both paths are in sync

## Upstream PRs (planned)

These patches are candidates for upstream PRs (would be sent to
[`Pickle-Pixel/ApplyPilot`](https://github.com/Pickle-Pixel/ApplyPilot)):

- [ ] `cli.py`: `tailor`/`cover`/`packet`/`purge` subcommands + `--since` flag
- [ ] `database.py`: `ensure_columns()` + `ensure_indexes()` + `purge_old_jobs()`
- [ ] `apply/launcher.py`: `preflight_check()` + dry-run DB protection
- [ ] `apply/ollama_agent.py`: cost cap

The `sites` analytics + dynamic blacklist + submit gate + notifier are
Boštjan-specific customizations and probably stay in the patches/ dir.

## Why we don't just fork ApplyPilot

The applypilot package is on PyPI as v0.3.0 and updated by Pickle-Pixel. A
fork would:
- Add maintenance burden (rebase on every upstream release)
- Need a custom pip index
- Complicate sharing applypilot with other projects

Patches are simpler: when v0.4.0 ships, run `./patches/apply_patches.sh`
on the new install, fix any merge conflicts, done.
