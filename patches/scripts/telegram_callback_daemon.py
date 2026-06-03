#!/usr/bin/env python3
"""
Telegram callback polling daemon (4.6 extension).

Polls Telegram's getUpdates endpoint for callback_query events from
[✅ Approve] / [❌ Decline] inline-keyboard buttons sent by
`applypilot.apply.notifier.notify_approval_needed()`.

When a button is tapped, this daemon:
  1. Resolves the callback_data back to a job URL (raw or hash)
  2. Calls `agents.auto_apply.mark_approval_approved()` or
     `mark_approval_declined()` in the DB
  3. Sends `answerCallbackQuery()` to clear the "loading" spinner
  4. Optionally edits the original message to show the result

The daemon writes its PID to /tmp/applypilot_telegram_listener.pid so a
CLI subcommand can stop it (see A6).

Usage:
    # Foreground (for debugging)
    python3 scripts/telegram_callback_daemon.py

    # As a background process (via CLI)
    python3 -m applypilot telegram-listener start
    python3 -m applypilot telegram-listener stop
    python3 -m applypilot telegram-listener status

Configuration:
    APPLY_TELEGRAM_POLL_INTERVAL   Seconds between getUpdates calls (default 1.5)
    APPLY_TELEGRAM_LONG_POLL       Use long-polling (default 1; set to 0 for short)
    APPLY_TELEGRAM_LONG_POLL_TIMEOUT  Long-poll timeout in seconds (default 25)
    APPLY_TELEGRAM_LISTENER_LOG   Path to daemon log (default /tmp/applypilot_telegram_listener.log)
    APPLY_TELEGRAM_LISTENER_PID   Path to PID file (default /tmp/applypilot_telegram_listener.pid)
    APPLY_TELEGRAM_EDIT_AFTER_CALLBACK  If 1, edit the original message after
                                        the button is pressed (default 1)

Notes:
- Telegram's getUpdates is exclusive with webhooks (you can't have both).
  If you have a webhook set up, this daemon will silently get nothing.
- We pass `allowed_updates=["callback_query"]` to getUpdates so the bot
  doesn't have to filter all other event types.
- The hash registry (for URLs longer than 64 bytes) is stored in
  ~/.applypilot/telegram_hash_registry.json. It maps "<sha256-prefix>" → URL.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# ── Paths and config ────────────────────────────────────────────────────────
# Hermes profile isolation sets $HOME to a sandboxed path that doesn't contain
# the user's actual files. We try several candidate APP_DIRs in order and pick
# the first that contains applypilot.db. (The real one is at
# /home/bostjan/.applypilot which is a symlink to the CV-automation project.)
def _resolve_app_dir() -> Path:
    """Return the first APP_DIR candidate that actually contains applypilot.db."""
    env = os.environ.get("APPLY_APPDIR", "").strip()
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.extend([
        Path("/home/bostjan") / ".applypilot",
        Path.home() / ".applypilot",
        Path("/home/bostjan") / ".hermes" / "profiles" / "osebno" / "home" / ".applypilot",
    ])
    for c in candidates:
        if (c / "applypilot.db").exists():
            return c
    # None of them have the DB — fall back to the first candidate and let it
    # be created (matches the original behaviour).
    return candidates[0]


APP_DIR = _resolve_app_dir()
APP_DIR.mkdir(parents=True, exist_ok=True)

LOG_PATH = Path(os.environ.get("APPLY_TELEGRAM_LISTENER_LOG",
                                "/tmp/applypilot_telegram_listener.log"))
PID_PATH = Path(os.environ.get("APPLY_TELEGRAM_LISTENER_PID",
                                "/tmp/applypilot_telegram_listener.pid"))
HASH_REGISTRY_PATH = APP_DIR / "telegram_hash_registry.json"
DB_PATH = APP_DIR / "applypilot.db"

POLL_INTERVAL = float(os.environ.get("APPLY_TELEGRAM_POLL_INTERVAL", "1.5"))
LONG_POLL = os.environ.get("APPLY_TELEGRAM_LONG_POLL", "1").strip() not in ("0", "false", "no")
LONG_POLL_TIMEOUT = int(os.environ.get("APPLY_TELEGRAM_LONG_POLL_TIMEOUT", "25"))
EDIT_AFTER = os.environ.get("APPLY_TELEGRAM_EDIT_AFTER_CALLBACK", "1").strip() not in ("0", "false", "no")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("telegram_listener")


# ── Signal handling ─────────────────────────────────────────────────────────
_running = True


def _stop(signum, frame) -> None:
    global _running
    logger.info("Received signal %s — stopping daemon", signum)
    _running = False


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


# ── Telegram API helpers ────────────────────────────────────────────────────
def _load_creds() -> tuple[str | None, str | None]:
    """Load bot token + chat id from env or the Hermes secrets file."""
    for token_name in ("APPLY_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"):
        token = os.environ.get(token_name, "").strip()
        if token:
            break
    else:
        token = None
    for chat_name in ("APPLY_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID"):
        chat_id = os.environ.get(chat_name, "").strip()
        if chat_id:
            break
    else:
        chat_id = None
    # Fallback 1: secrets file (try several candidate paths because the daemon
    # may run in a context where $HOME differs from /home/bostjan).
    if (not token or not chat_id):
        for sec_path in (
            Path("/home/bostjan") / ".hermes" / "secrets" / "telegram.json",
            Path.home() / ".hermes" / "secrets" / "telegram.json",
            Path("/home/bostjan") / ".applypilot" / "telegram.json",
        ):
            if sec_path.exists():
                try:
                    data = json.loads(sec_path.read_text())
                    token = token or data.get("bot_token")
                    cid = data.get("chat_id")
                    chat_id = chat_id or (str(cid) if cid else None)
                    break
                except Exception as e:
                    logger.warning("Failed to read %s: %s", sec_path, e)
    # Fallback 2: ~/.applypilot/.env (when env vars not set in this process)
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
                    logger.warning("Failed to read %s: %s", env_path, e)
                break
    return token, chat_id


def _telegram_get_updates(token: str, offset: int | None = None,
                          timeout: int = 25,
                          allowed: list[str] | None = None) -> list[dict]:
    """Call getUpdates. Uses long-polling if APPLY_TELEGRAM_LONG_POLL=1."""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {}
    if offset is not None:
        params["offset"] = offset
    if LONG_POLL and timeout:
        params["timeout"] = timeout
    if allowed:
        params["allowed_updates"] = json.dumps(allowed)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout + 10) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                return data.get("result", [])
            logger.warning("getUpdates returned ok=false: %s", data)
            return []
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.warning("getUpdates failed: %s", e)
        return []


def _answer_callback(token: str, callback_id: str, text: str = "",
                      show_alert: bool = False) -> bool:
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_id, "show_alert": show_alert}
    if text:
        payload["text"] = text[:200]
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("ok", False)
    except Exception as e:
        logger.warning("answerCallbackQuery failed: %s", e)
        return False


def _edit_message(token: str, chat_id: str, message_id: int,
                   new_text: str) -> bool:
    """Edit a message we sent (removes the inline keyboard too)."""
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": new_text,
        "parse_mode": "HTML",
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("ok", False)
    except Exception as e:
        logger.warning("editMessageText failed: %s", e)
        return False


def _send_text(token: str, chat_id: str, text: str,
               parse_mode: str = "HTML", timeout: float = 10.0) -> bool:
    """Plain sendMessage (no inline keyboard). Used for preview deliveries."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return data.get("ok", False)
    except Exception as e:
        logger.warning("sendMessage failed: %s", e)
        return False


def _send_document(token: str, chat_id: str, file_path: Path,
                   caption: str = "", timeout: float = 30.0) -> bool:
    """Upload a file as a Telegram document (used for PDF previews)."""
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    boundary = "----applypilot-preview-boundary"
    with open(file_path, "rb") as f:
        file_bytes = f.read()
    parts: list[bytes] = []
    # chat_id
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="chat_id"\r\n\r\n')
    parts.append(chat_id.encode() + b"\r\n")
    # document (file)
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="document"; filename="{file_path.name}"\r\n'.encode())
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
    parts.append(file_bytes + b"\r\n")
    # caption
    if caption:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(b'Content-Disposition: form-data; name="caption"\r\n\r\n')
        parts.append(caption.encode() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return data.get("ok", False)
    except Exception as e:
        logger.warning("sendDocument failed: %s", e)
        return False


# ── Hash registry (for URLs > 64 bytes) ────────────────────────────────────
def _load_hash_registry() -> dict[str, str]:
    if not HASH_REGISTRY_PATH.exists():
        return {}
    try:
        return json.loads(HASH_REGISTRY_PATH.read_text())
    except Exception as e:
        logger.warning("Failed to load hash registry: %s", e)
        return {}


def _save_hash_registry(reg: dict[str, str]) -> None:
    try:
        HASH_REGISTRY_PATH.write_text(json.dumps(reg, indent=2))
    except Exception as e:
        logger.warning("Failed to save hash registry: %s", e)


def _register_url_if_long(url: str, callback_data: str) -> None:
    """If callback_data uses the h:<hash> form, register the mapping.

    This is called from the notifier side (out-of-band). To keep things
    self-contained, the listener can also extract the hash and look it up
    in any registered URL that matches.
    """
    if "h:" not in callback_data:
        return
    h = callback_data.split("h:", 1)[1]
    reg = _load_hash_registry()
    if h not in reg:
        reg[h] = url
        _save_hash_registry(reg)


def _resolve_callback(callback_data: str, hash_registry: dict[str, str]) -> str | None:
    """Resolve a callback_data string back to a job URL.

    Accepts:
      - "approve:<url>" or "decline:<url>" (raw URL form)
      - "approve:h:<hash>" or "decline:h:<hash>" (hash form, looks up in registry)
    Returns the URL, or None if it can't be resolved.
    """
    if ":" not in callback_data:
        return None
    # Strip the prefix
    _, _, rest = callback_data.partition(":")
    if rest.startswith("h:"):
        # Hash form
        h = rest[2:]
        return hash_registry.get(h)
    return rest


# ── Approval action ─────────────────────────────────────────────────────────
def _do_approval(url: str, action: str, chat_id: str | None) -> str:
    """Run the approval or decline action. Returns a human-readable message."""
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from agents.auto_apply import mark_approval_approved, mark_approval_declined
    except Exception as e:
        return f"❌ Could not import auto_apply module: {e}"
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
    except Exception as e:
        return f"❌ DB open failed: {e}"
    try:
        if action == "approve":
            mark_approval_approved(conn, url, actor="telegram")
            return f"✅ Approved: {url[:60]}..."
        else:
            mark_approval_declined(conn, url)
            return f"❌ Declined: {url[:60]}..."
    except Exception as e:
        return f"❌ DB update failed: {e}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── Preview delivery (resume / cover letter) ─────────────────────────────────
# Telegram's sendMessage text limit is 4096 chars. For text files we truncate
# intelligently. For PDFs we send the file itself as a document (Telegram can
# render PDFs inline on iOS/Android) AND a short text summary so the user can
# decide without opening the file.
TELEGRAM_TEXT_LIMIT = 3800

# Telegram allows bot-uploaded documents up to 50 MB, but a multi-MB file
# trampled through urllib in a single read is enough to make the daemon hang
# on slow networks. Cap previews at 5 MB.
_MAX_PREVIEW_BYTES = 5 * 1024 * 1024


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


def _truncate_for_telegram(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    last_nl = truncated.rfind("\n")
    if last_nl > limit * 0.7:
        truncated = truncated[:last_nl]
    return truncated + "\n\n…(truncated — full file on disk)"


def _read_tailored_file(path: str) -> tuple[str | None, str | None]:
    """Read a tailored file (PDF or text). Returns (text, error).

    Files larger than ``_MAX_PREVIEW_BYTES`` (default 5 MB) are rejected
    so a single corrupt 500 MB PDF can't OOM the daemon.
    """
    if not path:
        return None, "no path on file for this job"
    p = Path(path)
    if not p.exists():
        return None, f"file missing: {p.name}"
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


def _lookup_file_for_url(url: str, kind: str) -> str | None:
    """Look up tailored_resume_path or cover_letter_path in the DB for a job."""
    col = "tailored_resume_path" if kind == "resume" else "cover_letter_path"
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        try:
            row = conn.execute(
                f"SELECT {col} FROM jobs WHERE url = ?", (url,)
            ).fetchone()
            return row[0] if row and row[0] else None
        finally:
            conn.close()
    except Exception as e:
        logger.warning("DB lookup for %s failed: %s", url, e)
        return None


def _lookup_title_for_url(url: str) -> str | None:
    """Look up the job title for a preview header."""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        try:
            row = conn.execute(
                "SELECT title FROM jobs WHERE url = ?", (url,)
            ).fetchone()
            return row[0] if row and row[0] else None
        finally:
            conn.close()
    except Exception as e:
        logger.warning("DB title lookup for %s failed: %s", url, e)
        return None


def _handle_preview(url: str, kind: str, token: str, chat_id: str,
                     title: str) -> str:
    """Send the resume or cover letter preview to the user. Returns ack text."""
    path = _lookup_file_for_url(url, kind)
    if not path:
        return f"⚠️ No {kind} on file for this job"
    p = Path(path)
    if not p.exists():
        return f"⚠️ {kind.capitalize()} file missing: {p.name}"

    # If PDF, send as a document so the user can open it in Telegram. Also
    # send a short text preview (first 1.5k chars) so they can decide quickly.
    if p.suffix.lower() == ".pdf":
        text, err = _read_tailored_file(path)
        if err:
            return f"⚠️ {err}"
        caption = f"{kind.capitalize()} for: {title}"
        sent_doc = _send_document(token, chat_id, p, caption=caption)
        if not sent_doc:
            return f"⚠️ Could not upload {p.name}"
        # Then send a short text summary
        summary = _truncate_for_telegram(text, limit=1500)
        body = f"📄 <b>{kind.capitalize()} (preview):</b> {_html_escape(title)}\n<pre>{_html_escape(summary)}</pre>"
        _send_text(token, chat_id, body)
        return f"📄 Sent {kind} (PDF + text preview)"

    # Text/markdown file — send inline
    text, err = _read_tailored_file(path)
    if err:
        return f"⚠️ {err}"
    header = f"📄 <b>Resume:</b> {_html_escape(title)}\n<pre>" if kind == "resume" \
             else f"✉️ <b>Cover letter:</b> {_html_escape(title)}\n<pre>"
    footer = "</pre>"
    budget = TELEGRAM_TEXT_LIMIT - len(header) - len(footer) - 4
    body = _truncate_for_telegram(text, limit=budget)
    message = header + _html_escape(body) + footer
    if _send_text(token, chat_id, message):
        return f"📄 Sent {kind} preview"
    return f"⚠️ Could not send {kind} preview"


# ── Main loop ──────────────────────────────────────────────────────────────
def main() -> int:
    token, chat_id = _load_creds()
    if not token:
        logger.error("No Telegram bot token configured. Set TELEGRAM_BOT_TOKEN "
                     "or APPLY_TELEGRAM_BOT_TOKEN in env, or write "
                     "~/.hermes/secrets/telegram.json")
        return 1

    # Write PID
    try:
        PID_PATH.write_text(str(os.getpid()))
    except Exception as e:
        logger.warning("Failed to write PID file: %s", e)

    logger.info("Telegram callback daemon started (pid=%d, chat_id=%s, "
                "long_poll=%s, interval=%.1fs)",
                os.getpid(), chat_id or "?", LONG_POLL, POLL_INTERVAL)

    offset: int | None = None
    hash_registry = _load_hash_registry()
    logger.info("Hash registry: %d entries", len(hash_registry))

    while _running:
        try:
            updates = _telegram_get_updates(
                token,
                offset=offset,
                timeout=LONG_POLL_TIMEOUT if LONG_POLL else 0,
                allowed=["callback_query"],
            )
        except Exception as e:
            logger.error("getUpdates raised: %s", e)
            time.sleep(POLL_INTERVAL)
            continue

        for update in updates:
            # Advance the offset past this update_id so we don't re-process
            offset = update["update_id"] + 1

            callback = update.get("callback_query")
            if not callback:
                continue

            callback_id = callback.get("id", "")
            data = callback.get("data", "")
            from_user = callback.get("from", {})
            username = from_user.get("username") or from_user.get("first_name") or "?"
            message = callback.get("message", {})
            message_chat_id = str(message.get("chat", {}).get("id", ""))
            message_id = message.get("message_id")

            # Optional: only process callbacks from our chat
            if chat_id and message_chat_id and message_chat_id != str(chat_id):
                logger.warning("Ignoring callback from chat %s (expected %s)",
                               message_chat_id, chat_id)
                _answer_callback(token, callback_id, text="Unauthorized chat")
                continue

            # Resolve URL and route by action
            action = None
            kind = None
            if data.startswith("approve:"):
                action = "approve"
            elif data.startswith("decline:"):
                action = "decline"
            elif data.startswith("view_resume:"):
                action = "view"; kind = "resume"
            elif data.startswith("view_cover:"):
                action = "view"; kind = "cover"
            else:
                logger.info("Ignoring unknown callback_data: %r", data)
                _answer_callback(token, callback_id, text="Unknown action")
                continue

            url = _resolve_callback(data, hash_registry)
            if not url:
                logger.warning("Could not resolve callback_data: %r", data)
                _answer_callback(token, callback_id,
                                 text="Could not find job. (Hash expired?)",
                                 show_alert=True)
                continue

            if action == "view":
                # Look up the job title for the preview header
                title = _lookup_title_for_url(url) or "(no title)"
                logger.info("Preview request from @%s: %s for %s",
                            username, kind, url[:80])
                result_msg = _handle_preview(url, kind, token,
                                             message_chat_id or chat_id or "",
                                             title)
                logger.info("Preview result: %s", result_msg)
                _answer_callback(token, callback_id, text=result_msg)
                # Don't edit the original message — buttons stay tappable so
                # the user can preview again or make a decision afterwards.
                continue

            logger.info("Callback from @%s: %s %s", username, action, url[:80])
            result_msg = _do_approval(url, action, chat_id)
            logger.info("Result: %s", result_msg)

            # Acknowledge the button tap (removes spinner)
            _answer_callback(token, callback_id, text=result_msg)

            # Edit the original message to reflect the decision
            if EDIT_AFTER and message_id and message_chat_id:
                emoji = "✅" if action == "approve" else "❌"
                decision = "Approved" if action == "approve" else "Declined"
                new_text = (
                    f"{emoji} <b>{decision} by @{_html_escape(username)}</b>\n\n"
                    f"   🔗 {_html_escape(url[:80])}\n\n"
                    f"<i>{_html_escape(result_msg)}</i>"
                )
                _edit_message(token, message_chat_id, message_id, new_text)

        # Short poll delay (only if we didn't long-poll)
        if not LONG_POLL:
            time.sleep(POLL_INTERVAL)

    # Cleanup
    try:
        PID_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    logger.info("Telegram callback daemon stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
