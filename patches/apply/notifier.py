"""
Telegram notification helper for applypilot.

Sends a short message to a configured Telegram chat when an application
is successfully submitted (or any other significant event). Failures are
non-fatal — the apply pipeline must not break because Telegram is down.

Configuration (env vars — first match wins):
    APPLY_TELEGRAM_BOT_TOKEN / TELEGRAM_BOT_TOKEN
        — bot token from @BotFather
    APPLY_TELEGRAM_CHAT_ID / TELEGRAM_CHAT_ID
        — chat ID (numeric; get from @userinfobot, or by sending a message
          to your bot and running getUpdates)
    APPLY_TELEGRAM_DISABLE=1
        — kill switch (no messages sent even if token set)

We also read ~/.hermes/secrets/telegram.json as a fallback location. The
file format is:
    {"bot_token": "...", "chat_id": 123456789}

If neither env nor file is configured, the notifier logs a debug message
and returns False (silent no-op).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Module-level cache for token/chat id, so we don't re-read on every call.
_CACHED_TOKEN: str | None = None
_CACHED_CHAT_ID: str | None = None
_CACHE_LOADED = False

# Default location: Hermes secrets (sibling of gmb_bklajnscak_token.json)
_SECRETS_PATH = Path.home() / ".hermes" / "secrets" / "telegram.json"


def _first_env(*names: str) -> str | None:
    """Return the first non-empty value from any of the given env var names."""
    for name in names:
        v = os.environ.get(name, "").strip()
        if v:
            return v
    return None


def _load_creds() -> tuple[str | None, str | None]:
    """Load bot token + chat id. Returns (None, None) if not configured."""
    global _CACHED_TOKEN, _CACHED_CHAT_ID, _CACHE_LOADED
    if _CACHE_LOADED:
        return _CACHED_TOKEN, _CACHED_CHAT_ID

    # Kill switch
    if os.environ.get("APPLY_TELEGRAM_DISABLE", "").strip() in ("1", "true", "yes"):
        logger.info("Telegram notifier: APPLY_TELEGRAM_DISABLE set, skipping")
        _CACHED_TOKEN = None
        _CACHED_CHAT_ID = None
        _CACHE_LOADED = True
        return None, None

    # Try multiple env var name conventions.
    # The most common .env convention is plain TELEGRAM_BOT_TOKEN.
    token = _first_env(
        "APPLY_TELEGRAM_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
    )
    chat_id = _first_env(
        "APPLY_TELEGRAM_CHAT_ID",
        "TELEGRAM_CHAT_ID",
    )

    # Fallback to secrets file
    if (not token or not chat_id) and _SECRETS_PATH.exists():
        try:
            data = json.loads(_SECRETS_PATH.read_text())
            token = token or data.get("bot_token")
            cid = data.get("chat_id")
            chat_id = chat_id or (str(cid) if cid else None)
        except Exception as e:
            logger.warning("Telegram notifier: failed to read %s: %s", _SECRETS_PATH, e)

    _CACHED_TOKEN = token
    _CACHED_CHAT_ID = chat_id
    _CACHE_LOADED = True
    if not token or not chat_id:
        logger.debug("Telegram notifier: no credentials configured (set TELEGRAM_BOT_TOKEN/CHAT_ID or write %s)", _SECRETS_PATH)
    return token, chat_id


def reset_cache() -> None:
    """Clear the cached credentials (for tests)."""
    global _CACHED_TOKEN, _CACHED_CHAT_ID, _CACHE_LOADED
    _CACHED_TOKEN = None
    _CACHED_CHAT_ID = None
    _CACHE_LOADED = False


def _send_telegram_message(token: str, chat_id: str, text: str,
                            parse_mode: str = "HTML",
                            disable_web_preview: bool = True,
                            timeout: float = 10.0) -> bool:
    """Send a message via the Telegram Bot API. Returns True on success."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_preview,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                return True
            logger.warning("Telegram API returned ok=false: %s", data)
            return False
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        logger.warning("Telegram send failed: %s", e)
        return False


def _html_escape(s: str) -> str:
    """Minimal HTML escape for Telegram HTML parse mode."""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


def notify_applied(job: dict, duration_ms: int | None = None,
                    cost_usd: float | None = None) -> bool:
    """Send a Telegram notification for a successful application.

    Args:
        job: dict with at least 'url', 'title', 'company', 'site' (any may be None)
        duration_ms: how long the apply took (for the message footer)
        cost_usd: estimated LLM cost (for the message footer)

    Returns:
        True if the message was sent, False if not configured or send failed.
    """
    token, chat_id = _load_creds()
    if not token or not chat_id:
        return False

    title = _html_escape(str(job.get("title") or "(no title)"))
    company = _html_escape(str(job.get("company") or "(unknown)"))
    site = _html_escape(str(job.get("site") or "unknown"))
    url = str(job.get("url") or job.get("application_url") or "")
    # Telegram will show the URL as a preview link
    url_escaped = _html_escape(url)

    parts = ["✅ <b>Applied:</b> {title}".format(title=title)]
    if company and company != "(unknown)":
        parts.append(f"   🏢 {company}")
    if site and site != "unknown":
        parts.append(f"   🌐 {site}")
    if url:
        parts.append(f"   🔗 {url_escaped}")
    if duration_ms is not None:
        parts.append(f"   ⏱ {duration_ms / 1000:.1f}s")
    if cost_usd is not None and cost_usd > 0:
        parts.append(f"   💰 ${cost_usd:.3f}")

    text = "\n".join(parts)

    # Send in a background thread so it doesn't block the apply pipeline.
    def _send() -> None:
        ok = _send_telegram_message(token, chat_id, text)
        if not ok:
            logger.warning("Telegram notify_applied: send failed for %s", url[:80])

    threading.Thread(target=_send, daemon=True).start()
    return True


def notify_failed(job: dict, error: str | None = None) -> bool:
    """Send a Telegram notification for a failed application.

    Only fires if APPLY_TELEGRAM_NOTIFY_FAIL=1 is set (off by default to
    avoid spam from broken sites). Same threading/non-blocking model.
    """
    if os.environ.get("APPLY_TELEGRAM_NOTIFY_FAIL", "").strip() not in ("1", "true", "yes"):
        return False

    token, chat_id = _load_creds()
    if not token or not chat_id:
        return False

    title = _html_escape(str(job.get("title") or "(no title)"))
    site = _html_escape(str(job.get("site") or "unknown"))
    error_esc = _html_escape((error or "")[:200])

    text = (
        f"❌ <b>Apply failed:</b> {title}\n"
        f"   🌐 {site}\n"
        f"   ⚠ {error_esc}"
    )

    def _send() -> None:
        _send_telegram_message(token, chat_id, text)

    threading.Thread(target=_send, daemon=True).start()
    return True
