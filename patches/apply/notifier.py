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
# We try several locations because the bot can run in different contexts
# (Hermes profile isolation sets $HOME to a fake home dir).
_SECRETS_CANDIDATES = [
    Path.home() / ".hermes" / "secrets" / "telegram.json",
    Path("/home/bostjan") / ".hermes" / "secrets" / "telegram.json",
    Path("/home/bostjan") / ".applypilot" / "telegram.json",
    Path("/home/bostjan") / ".applypilot" / ".telegram.json",
]
# Keep backwards-compatible name pointing at the first candidate
_SECRETS_PATH = _SECRETS_CANDIDATES[0]


def _find_secrets_file() -> Path | None:
    """Return the first existing secrets-file path, or None."""
    for p in _SECRETS_CANDIDATES:
        if p.exists():
            return p
    return None


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

    # Fallback 1: secrets file (try several candidate paths)
    if (not token or not chat_id):
        sp = _find_secrets_file()
        if sp is not None:
            try:
                data = json.loads(sp.read_text())
                token = token or data.get("bot_token")
                cid = data.get("chat_id")
                chat_id = chat_id or (str(cid) if cid else None)
            except Exception as e:
                logger.warning("Telegram notifier: failed to read %s: %s", sp, e)

    # Fallback 2: ~/.applypilot/.env (only if env vars are still missing).
    # The notifier's caller may have already loaded .env into os.environ, so
    # we only consult the file directly when both token and chat_id are absent.
    if not token or not chat_id:
        for env_path in (
            Path("/home/bostjan") / ".applypilot" / ".env",
            Path.home() / ".applypilot" / ".env",
        ):
            if env_path.exists():
                try:
                    for line in env_path.read_text().splitlines():
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, _, v = line.partition("=")
                        k, v = k.strip(), v.strip().strip('"').strip("'")
                        if k in ("TELEGRAM_BOT_TOKEN", "APPLY_TELEGRAM_BOT_TOKEN") and v and not token:
                            token = v
                        elif k in ("TELEGRAM_CHAT_ID", "APPLY_TELEGRAM_CHAT_ID") and v and not chat_id:
                            chat_id = v
                except Exception as e:
                    logger.warning("Telegram notifier: failed to read %s: %s", env_path, e)
                break

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
                            reply_markup: dict | None = None,
                            timeout: float = 10.0) -> bool:
    """Send a message via the Telegram Bot API. Returns True on success.

    Args:
        token: Bot token
        chat_id: Numeric chat id
        text: Message body
        parse_mode: "HTML" or "MarkdownV2"
        disable_web_preview: True hides link previews
        reply_markup: Optional inline-keyboard markup dict, e.g.
            {"inline_keyboard": [[{"text": "Yes", "callback_data": "approve:..."}]]}
        timeout: HTTP timeout in seconds
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_preview,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
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


# -------------------------------------------------------------------
# Inline-keyboard approval flow (4.6 extension)
# -------------------------------------------------------------------
# Telegram inline-keyboard buttons can carry at most 64 bytes of callback_data.
# We use the prefix scheme: "approve:<url>" or "decline:<url>". Long URLs are
# hashed to fit; the listener daemon (scripts/telegram_callback_daemon.py)
# maintains a hash→url registry so it can resolve them back.
#
# When a button is tapped, Telegram sends a callback_query to the bot. Our
# polling daemon picks it up, calls agents.auto_apply.mark_approval_approved()
# or mark_approval_declined(), and answers the callback with a brief ack.

import hashlib as _hashlib

# Reserved callback_data prefixes (don't use these in regular messages)
# NOTE: these do NOT end with ':' — the prefix-separator colon is added by
# the formatter. This keeps the format uniform: <prefix>:<url> or
# <prefix>:h:<hash>. The resolver uses partition(':') which always splits
# on the first colon, so this format is unambiguous.
CALLBACK_APPROVE     = "approve"
CALLBACK_DECLINE     = "decline"
CALLBACK_VIEW_RESUME = "view_resume"
CALLBACK_VIEW_COVER  = "view_cover"

# Telegram sendMessage text limit is 4096 chars; we leave headroom.
TELEGRAM_TEXT_LIMIT = 3800

# Bot-uploaded documents cap at 50 MB but we cap previews lower so a single
# huge file can't hang the notifier thread on a slow network.
_MAX_PREVIEW_BYTES = 5 * 1024 * 1024


def _url_to_callback_data(prefix: str, url: str) -> str:
    """Build a callback_data string of at most 64 bytes.

    Output format (uniform across all action types):
      - raw:    ``<prefix>:<url>``            (when URL fits in the budget)
      - hashed: ``<prefix>:h:<12-hex-chars>`` (when URL is too long)

    The colon after the prefix is the only separator. The resolver calls
    ``partition(':')`` to split prefix from payload — so the hash form
    is always recognised by checking if the payload starts with ``"h:"``.

    Note: callers must pass a prefix WITHOUT a trailing colon. The constants
    ``CALLBACK_APPROVE`` etc. already follow this convention.
    """
    hash_overhead = 3 + 12  # ':h:' + 12 hex chars
    max_url_bytes = 64 - len(prefix.encode("utf-8")) - 1 - hash_overhead  # -1 for ':'
    if len(url.encode("utf-8")) <= max_url_bytes:
        return f"{prefix}:{url}"
    h = _hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:h:{h}"


def _approval_markup(url: str) -> dict:
    """Build the inline-keyboard markup for an approval request.

    Returns a dict suitable for the `reply_markup` parameter of sendMessage.
    The keyboard is 2 rows × 2 buttons:
      Row 1: [📄 Resume] [✉️ Cover letter]
      Row 2: [✅ Approve] [❌ Decline]
    """
    return {
        "inline_keyboard": [
            [
                {"text": "📄 Resume",   "callback_data": _url_to_callback_data(CALLBACK_VIEW_RESUME, url)},
                {"text": "✉️ Cover",    "callback_data": _url_to_callback_data(CALLBACK_VIEW_COVER, url)},
            ],
            [
                {"text": "✅ Approve",  "callback_data": _url_to_callback_data(CALLBACK_APPROVE, url)},
                {"text": "❌ Decline",  "callback_data": _url_to_callback_data(CALLBACK_DECLINE, url)},
            ],
        ]
    }


def _read_tailored_file(path: str, kind: str) -> tuple[str | None, str | None]:
    """Read a tailored file (PDF or text) and return (text, error).

    Files larger than ``_MAX_PREVIEW_BYTES`` (default 5 MB) are rejected so a
    single corrupt huge PDF can't exhaust memory in the background thread.

    Returns:
        (text, None) on success — `text` is HTML-escaped and truncated to fit
                                  Telegram's 4096-char message limit.
        (None, error) on failure — `error` is a short human-readable reason.
    """
    if not path:
        return None, f"no {kind} on file for this job"
    p = Path(path)
    if not p.exists():
        return None, f"{kind.capitalize()} file missing: {p.name}"
    try:
        size = p.stat().st_size
    except OSError as e:
        return None, f"could not stat {p.name}: {e}"
    if size > _MAX_PREVIEW_BYTES:
        return None, (
            f"{p.name} is {size // 1024} KB — too large to preview "
            f"(limit is {_MAX_PREVIEW_BYTES // 1024} KB)"
        )
    try:
        if p.suffix.lower() == ".pdf":
            # Try pypdf first; fall back to a best-effort raw read.
            try:
                from pypdf import PdfReader
                reader = PdfReader(str(p))
                chunks = [(page.extract_text() or "") for page in reader.pages]
                text = "\n\n".join(chunks).strip()
            except Exception as e:
                logger.warning("pypdf failed on %s: %s; falling back to raw read", p, e)
                text = p.read_text(errors="ignore")
        else:
            text = p.read_text(errors="ignore")
    except Exception as e:
        return None, f"could not read {p.name}: {e}"
    if not text.strip():
        return None, f"{p.name} is empty"
    return text, None


def _truncate_for_telegram(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    """Truncate text to fit Telegram's message limit, ending on a whole line."""
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    # Try to end on the last newline so we don't slice a word in half.
    last_nl = truncated.rfind("\n")
    if last_nl > limit * 0.7:
        truncated = truncated[:last_nl]
    return truncated + "\n\n…(truncated — full file on disk)"


def format_preview_message(kind: str, title: str, body: str) -> str:
    """Build the HTML message body for a preview delivery.

    Wraps the file contents in a header + footer, truncates to the Telegram
    limit, and HTML-escapes only the wrapping text (the body is treated as
    preformatted plain text).
    """
    header = f"📄 <b>Resume:</b> {_html_escape(title)}\n" if kind == "resume" \
             else f"✉️ <b>Cover letter:</b> {_html_escape(title)}\n"
    header += "<pre>"
    footer = "</pre>"
    budget = TELEGRAM_TEXT_LIMIT - len(header) - len(footer) - 4
    body_truncated = _truncate_for_telegram(body, limit=budget)
    return header + _html_escape(body_truncated) + footer


def notify_approval_needed(job: dict) -> bool:
    """Send a Telegram notification asking the user to approve an application.

    Includes an inline keyboard with [✅ Approve] [❌ Decline] buttons. The
    callback is processed by scripts/telegram_callback_daemon.py, which
    calls mark_approval_approved() or mark_approval_declined() in the DB.

    Args:
        job: dict with at least 'url', 'title', 'company', 'site'

    Returns:
        True if the message was sent, False if not configured or send failed.
    """
    token, chat_id = _load_creds()
    if not token or not chat_id:
        return False

    url = str(job.get("url") or job.get("application_url") or "")
    if not url:
        logger.warning("notify_approval_needed: no URL in job dict, skipping")
        return False

    title = _html_escape(str(job.get("title") or "(no title)"))
    company = _html_escape(str(job.get("company") or "(unknown)"))
    site = _html_escape(str(job.get("site") or "unknown"))
    score = job.get("fit_score")
    score_str = f"{score}/10" if score is not None else "?"

    parts = [f"⏳ <b>Approval needed:</b> {title}"]
    if company and company != "(unknown)":
        parts.append(f"   🏢 {company}")
    if site and site != "unknown":
        parts.append(f"   🌐 {site}")
    parts.append(f"   📊 Fit score: {score_str}")
    parts.append("")
    parts.append("Tap a button to preview, approve or decline.")

    text = "\n".join(parts)
    markup = _approval_markup(url)

    def _send() -> None:
        ok = _send_telegram_message(token, chat_id, text, reply_markup=markup)
        if not ok:
            logger.warning("Telegram notify_approval_needed: send failed for %s", url[:80])

    threading.Thread(target=_send, daemon=True).start()
    return True


def answer_callback_query(callback_query_id: str,
                          text: str = "",
                          show_alert: bool = False,
                          timeout: float = 5.0) -> bool:
    """Acknowledge a callback_query (removes the "loading" spinner on the button).

    Required by the Telegram Bot API after every button tap. The polling
    daemon calls this immediately after processing the callback.
    """
    token, chat_id = _load_creds()
    if not token:
        return False
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        payload["text"] = text[:200]
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return data.get("ok", False)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        logger.warning("Telegram answerCallbackQuery failed: %s", e)
        return False

