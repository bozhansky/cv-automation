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
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ── Paths and config ────────────────────────────────────────────────────────
APP_DIR = Path(os.environ.get("APPLY_APPDIR", Path.home() / ".applypilot"))
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
    # Fallback to secrets file
    if (not token or not chat_id):
        sec_path = Path.home() / ".hermes" / "secrets" / "telegram.json"
        if sec_path.exists():
            try:
                data = json.loads(sec_path.read_text())
                token = token or data.get("bot_token")
                cid = data.get("chat_id")
                chat_id = chat_id or (str(cid) if cid else None)
            except Exception as e:
                logger.warning("Failed to read %s: %s", sec_path, e)
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

            # Resolve URL
            if data.startswith("approve:"):
                action = "approve"
            elif data.startswith("decline:"):
                action = "decline"
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


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


if __name__ == "__main__":
    sys.exit(main())
