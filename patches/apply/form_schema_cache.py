"""
Per-site form schema caching (4.8).

Once the apply agent successfully navigates an application form on a site
(e.g. LinkedIn, Indeed), save the discovered form structure to a JSON cache
keyed by site. On the next apply to the same site, inject the cached schema
into the prompt so the agent can skip 2-3 turns of "what fields does this
form have?" discovery.

Cache location:
    ~/.applypilot/form_schema_cache.json  (or APP_DIR/form_schema_cache.json)

Schema format (per site):
    {
      "<site>": {
        "form_url_pattern": "linkedin.com/jobs/view",
        "fields": [
          {
            "purpose": "email",
            "selector": "input[name='email']",
            "type": "input",
            "fallback_selectors": ["input[type='email']", "#email"],
            "value_source": "profile.email",
            "notes": "Sometimes a div with role=textbox instead of input"
          },
          ...
        ],
        "submit_button": {
          "selector": "button[type='submit']",
          "text": "Submit application"
        },
        "updated_at": "2026-06-02T15:00:00Z",
        "uses": 12,
        "successes": 10,
        "failures": 2
      }
    }

The cache is loaded on every apply and updated when:
  1. The agent successfully submits (successes++)
  2. The agent reports a new selector for a field (via a dedicated tool)
  3. The cache is older than 30 days AND the site has had no successful
     applies since (stale entry detection)

Public API:
    get_schema(site) -> dict | None
    save_schema(site, schema) -> None
    update_usage(site, success: bool) -> None
    get_cache_stats() -> dict
    prune_stale(threshold_days=30, min_attempts=3) -> int  (returns # pruned)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default cache location: alongside the DB
_CACHE_FILENAME = "form_schema_cache.json"
_cache_lock = threading.Lock()
_cache_data: dict[str, dict] | None = None
_cache_path: Path | None = None


def _resolve_cache_path() -> Path:
    """Resolve the cache file path. Uses config.APP_DIR if available, else ~/.applypilot."""
    global _cache_path
    if _cache_path is not None:
        return _cache_path
    try:
        from applypilot import config
        _cache_path = Path(config.APP_DIR) / _CACHE_FILENAME
    except (ImportError, AttributeError):
        _cache_path = Path.home() / ".applypilot" / _CACHE_FILENAME
    _cache_path.parent.mkdir(parents=True, exist_ok=True)
    return _cache_path


def _load_cache() -> dict[str, dict]:
    """Load the cache from disk. Returns empty dict if missing/corrupt."""
    global _cache_data
    with _cache_lock:
        if _cache_data is not None:
            return _cache_data
        path = _resolve_cache_path()
        if not path.exists():
            _cache_data = {}
            return _cache_data
        try:
            _cache_data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Form schema cache: failed to load %s: %s", path, e)
            _cache_data = {}
        return _cache_data


def _save_cache() -> None:
    """Persist the in-memory cache to disk. Caller must hold _cache_lock."""
    if _cache_data is None:
        return
    path = _resolve_cache_path()
    try:
        # Atomic write: write to .tmp then rename
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(_cache_data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("Form schema cache: failed to save %s: %s", path, e)


def reset_cache_for_testing() -> None:
    """Clear in-memory cache (for tests)."""
    global _cache_data
    with _cache_lock:
        _cache_data = None


def get_schema(site: str | None) -> dict | None:
    """Return the cached schema for `site`, or None if not cached."""
    if not site:
        return None
    cache = _load_cache()
    return cache.get(site)


def save_schema(site: str, schema: dict) -> None:
    """Save (or replace) the schema for a site.

    Args:
        site: site name (e.g. 'linkedin')
        schema: dict with keys: form_url_pattern, fields, submit_button,
                plus auto-added updated_at + zeroed uses/successes/failures
    """
    if not site:
        return
    cache = _load_cache()
    with _cache_lock:
        # Preserve usage stats if updating an existing entry
        existing = cache.get(site, {})
        schema.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
        schema.setdefault("uses", existing.get("uses", 0))
        schema.setdefault("successes", existing.get("successes", 0))
        schema.setdefault("failures", existing.get("failures", 0))
        cache[site] = schema
        _save_cache()


def update_usage(site: str, success: bool) -> None:
    """Increment usage counters for a site (called after each apply)."""
    if not site:
        return
    cache = _load_cache()
    with _cache_lock:
        if site not in cache:
            return  # No cached schema to update
        cache[site]["uses"] = cache[site].get("uses", 0) + 1
        if success:
            cache[site]["successes"] = cache[site].get("successes", 0) + 1
        else:
            cache[site]["failures"] = cache[site].get("failures", 0) + 1
        _save_cache()


def get_cache_stats() -> dict:
    """Return summary statistics about the cache (for dashboards)."""
    cache = _load_cache()
    total_sites = len(cache)
    total_uses = sum(s.get("uses", 0) for s in cache.values())
    total_success = sum(s.get("successes", 0) for s in cache.values())
    total_fail = sum(s.get("failures", 0) for s in cache.values())
    return {
        "total_sites": total_sites,
        "total_uses": total_uses,
        "total_successes": total_success,
        "total_failures": total_fail,
        "overall_success_rate": (total_success / total_uses) if total_uses else 0.0,
        "sites": sorted(cache.keys()),
    }


def prune_stale(threshold_days: int = 30, min_attempts: int = 3) -> int:
    """Remove stale cache entries: old (>= threshold_days) AND no successful
    apply in that window. Returns the number of entries pruned.
    """
    cache = _load_cache()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=threshold_days)).isoformat()
    pruned = 0
    with _cache_lock:
        for site in list(cache.keys()):
            entry = cache[site]
            updated_at = entry.get("updated_at", "")
            if not updated_at or updated_at >= cutoff:
                continue  # Recent — keep
            # If the site has had enough attempts but zero successes in
            # the stale window, prune.
            uses = entry.get("uses", 0)
            successes = entry.get("successes", 0)
            if uses >= min_attempts and successes == 0:
                logger.info("Pruning stale form schema for %s (uses=%d, success=0, age>=%dd)",
                            site, uses, threshold_days)
                del cache[site]
                pruned += 1
        if pruned:
            _save_cache()
    return pruned


def get_schema_for_prompt(site: str | None) -> str:
    """Return a human-readable summary of the cached schema for `site`,
    suitable for injection into the apply agent's prompt. Empty string if
    no cached schema.
    """
    schema = get_schema(site)
    if not schema:
        return ""
    parts = [f"\n## Cached form schema for {site} (re-use these selectors to save turns):"]
    parts.append(f"Form URL pattern: `{schema.get('form_url_pattern', 'unknown')}`")
    parts.append("Fields:")
    for f in schema.get("fields", []):
        purpose = f.get("purpose", "?")
        selector = f.get("selector", "?")
        fallbacks = f.get("fallback_selectors", [])
        notes = f.get("notes", "")
        line = f"  - {purpose}: `{selector}`"
        if fallbacks:
            line += f" (fallback: {', '.join('`' + s + '`' for s in fallbacks)})"
        if notes:
            line += f" — {notes}"
        parts.append(line)
    submit = schema.get("submit_button", {})
    if submit:
        parts.append(f"Submit: `{submit.get('selector', '?')}` (text='{submit.get('text', 'Submit')}')")
    parts.append(f"Cache stats: uses={schema.get('uses', 0)}, "
                 f"successes={schema.get('successes', 0)}, "
                 f"failures={schema.get('failures', 0)}")
    return "\n".join(parts)


# -------------------------------------------------------------------
# CLI subcommand
# -------------------------------------------------------------------
def _cli_main() -> None:
    """CLI: `python3 -m applypilot.form_schema_cache` (or just import this)."""
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "stats":
        import json as _json
        print(_json.dumps(get_cache_stats(), indent=2))
    elif cmd == "list":
        cache = _load_cache()
        for site, schema in sorted(cache.items()):
            print(f"\n=== {site} ===")
            print(f"  URL pattern: {schema.get('form_url_pattern', '?')}")
            print(f"  Fields: {len(schema.get('fields', []))}")
            print(f"  Uses: {schema.get('uses', 0)} ({schema.get('successes', 0)} success / {schema.get('failures', 0)} fail)")
            print(f"  Updated: {schema.get('updated_at', '?')}")
    elif cmd == "prune":
        n = prune_stale()
        print(f"Pruned {n} stale entries.")
    elif cmd == "show" and len(sys.argv) > 2:
        schema = get_schema(sys.argv[2])
        if schema:
            print(json.dumps(schema, indent=2))
        else:
            print(f"No schema cached for '{sys.argv[2]}'")
    else:
        print(f"Usage: {sys.argv[0]} [stats|list|prune|show <site>]")
        sys.exit(1)


if __name__ == "__main__":
    _cli_main()
