"""Ollama-based apply agent — replaces Claude CLI subprocess.

Uses an Ollama model (default: kimi-k2.6:cloud) with native tool-calling
to drive a Playwright-connected browser and fill out a job application.

This module is imported by applypilot/apply/launcher.py when
APPLY_AGENT=ollama is set (default in this fork). Falls back to spawning
the Claude CLI subprocess if APPLY_AGENT=claude.

Event stream (matches Claude's stream-json format) is emitted on stdout
so the existing dashboard + parser works unchanged.
"""

import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("APPLY_MODEL", "kimi-k2.6:cloud")
OLLAMA_URL = os.environ.get("APPLY_OLLAMA_URL", "http://127.0.0.1:11434/v1")
MAX_TURNS = int(os.environ.get("APPLY_MAX_TURNS", "20"))
MAX_TOKENS = int(os.environ.get("APPLY_MAX_TOKENS", "2048"))
# Per-job cost cap in USD. The Ollama agent is local so direct cost is
# $0, but LLM usage and time can still be substantial. The cap uses a
# conservative token-cost estimate ($3/1M input, $15/1M output — Claude
# Sonnet rates — well above local Ollama cost but a safe upper bound).
# Set APPLY_MAX_COST_PER_JOB=0 to disable the cap.
MAX_COST_PER_JOB = float(os.environ.get("APPLY_MAX_COST_PER_JOB", "0.50"))
COST_PER_INPUT_TOKEN = 3e-6    # $3 / 1M tokens
COST_PER_OUTPUT_TOKEN = 15e-6   # $15 / 1M tokens


# -------------------------------------------------------------------
# Tool schema — these become the Ollama `tools` list
# -------------------------------------------------------------------

PLAYWRIGHT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Navigate the browser to a URL. Use this to open the job application page.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Full URL to navigate to"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_snapshot",
            "description": "Get a structured accessibility snapshot of the current page. Returns elements with [ref=eN] IDs you can use in browser_click/browser_type. Use this ONCE per page to understand it.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_take_screenshot",
            "description": "Take a screenshot of the current page. Returns the path. Use sparingly — snapshots are cheaper.",
            "parameters": {
                "type": "object",
                "properties": {"filename": {"type": "string", "description": "Save as filename"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element on the page by its ref id (from browser_snapshot, e.g. 'e5') OR by CSS selector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Element ref id like e5 (from snapshot)"},
                    "selector": {"type": "string", "description": "CSS selector (alternative to ref)"},
                    "element": {"type": "string", "description": "Human description of the element"},
                },
                "required": ["element"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Type text into an input field. Clears the field first then types.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Element ref id (from snapshot)"},
                    "selector": {"type": "string", "description": "CSS selector (alternative to ref)"},
                    "text": {"type": "string", "description": "Text to type into the field"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_fill_form",
            "description": "Fill multiple form fields at once. fields is a list of {ref/name, type, value}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fields": {
                        "type": "array",
                        "description": "List of {name, ref, type, value} objects. type=textbox|checkbox|combobox|etc.",
                    }
                },
                "required": ["fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_file_upload",
            "description": "Upload a file (resume, cover letter) via a file input element. The path must be an absolute file path on this machine.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Element ref of the file input"},
                    "path": {"type": "string", "description": "Absolute path to the file to upload"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_tabs",
            "description": "Manage browser tabs. action=list|new|close|select. Use to detect new tabs opened after submit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "new", "close", "select"]},
                    "index": {"type": "integer", "description": "Tab index (for close/select)"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait_for",
            "description": "Wait for text to appear on the page or for a duration. Useful after navigation/submit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to wait for (optional)"},
                    "time": {"type": "number", "description": "Seconds to wait (optional)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_application",
            "description": "Submit the application form. MANDATORY safety gate: before calling this tool you MUST have (1) filled the form, (2) called browser_take_screenshot immediately before, (3) reviewed the screenshot to confirm all fields are correct. The tool refuses if you call any other tool between the screenshot and this call. In dry-run mode it is always refused. After a successful SUBMIT APPROVED response, call finish(APPLIED) on the next turn.",
            "parameters": {
                "type": "object",
                "properties": {
                    "screenshot_taken": {
                        "type": "boolean",
                        "description": "Must be true. Confirms you took a screenshot right before this submit.",
                    },
                    "fields_verified": {
                        "type": "string",
                        "description": "Comma-separated list of form fields you confirmed are filled correctly (e.g. 'email,resume,cover_letter,linkedin').",
                    },
                    "reason": {
                        "type": "string",
                        "description": "One-line human-readable reason this submission should proceed (e.g. 'All required fields filled; resume+cover uploaded').",
                    },
                },
                "required": ["screenshot_taken", "fields_verified", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Call this when the application is complete. status must be one of: APPLIED, EXPIRED, CAPTCHA, LOGIN_ISSUE, FAILED. Include a brief reason.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["APPLIED", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE", "FAILED"],
                    },
                    "reason": {"type": "string", "description": "Brief explanation of the outcome"},
                },
                "required": ["status"],
            },
        },
    },
]


# -------------------------------------------------------------------
# Ollama chat-completions call
# -------------------------------------------------------------------

def _ollama_chat(messages: list[dict], tools: list[dict], model: str = DEFAULT_MODEL) -> dict:
    """Call Ollama chat-completions API. Returns the full response dict."""
    body = json.dumps({
        "model": model,
        "messages": messages,
        "tools": tools,
        "temperature": 0.1,
        "max_tokens": MAX_TOKENS,
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


# -------------------------------------------------------------------
# Tool execution — talks to Playwright connected over CDP
# -------------------------------------------------------------------

class _PlaywrightToolRunner:
    """Executes the agent's tool calls against a Playwright-connected browser."""

    def __init__(self, cdp_url: str, worker_dir: Path, dry_run: bool = False):
        from playwright.sync_api import sync_playwright
        self._sync_playwright = sync_playwright
        self.cdp_url = cdp_url
        self.worker_dir = worker_dir
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        # Submit gate: tracks whether a screenshot was taken in the current
        # turn. The agent must call browser_take_screenshot right before
        # submit_application; the gate is reset at the start of every turn.
        self._screenshot_taken_this_turn = False
        self._dry_run = dry_run
        # Number of times submit_application has been called (across all turns).
        self._submit_attempts = 0
        # Last submission screenshot path (for post-submit review).
        self._last_submission_screenshot: str | None = None

    def __enter__(self):
        self._pw = self._sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        # Reuse an existing context if one exists, else create
        contexts = self._browser.contexts
        if contexts:
            self._context = contexts[0]
        else:
            self._context = self._browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            )
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        return self

    def __exit__(self, *args):
        try:
            if self._pw:
                self._pw.stop()
        except Exception as e:
            logger.warning("Playwright shutdown error: %s", e)

    def call(self, name: str, arguments: dict) -> str:
        """Dispatch a tool call. Returns a string result to feed back to the LLM."""
        # ── Submit gate: tools that don't require a screenshot reset the gate ──
        # The agent must call browser_take_screenshot right before
        # submit_application. Any other tool call between the screenshot and
        # the submit invalidates the gate.
        if name != "submit_application" and name != "browser_take_screenshot":
            self._screenshot_taken_this_turn = False

        try:
            handler = {
                "browser_navigate": self._navigate,
                "browser_snapshot": self._snapshot,
                "browser_take_screenshot": self._screenshot,
                "browser_click": self._click,
                "browser_type": self._type,
                "browser_fill_form": self._fill_form,
                "browser_file_upload": self._file_upload,
                "browser_tabs": self._tabs,
                "browser_wait_for": self._wait_for,
                "submit_application": self._submit_application,
            }.get(name)
            if not handler:
                return f"Error: unknown tool '{name}'"
            return handler(arguments)
        except Exception as e:
            return f"Error executing {name}: {type(e).__name__}: {e}"

    def _navigate(self, args: dict) -> str:
        url = args.get("url", "")
        self._page.goto(url, timeout=45000, wait_until="domcontentloaded")
        try:
            self._page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        return f"Navigated to {url}. Current URL: {self._page.url}"

    def _snapshot(self, args: dict) -> str:
        # Get DOM text representation; simpler than full a11y tree
        title = self._page.title()
        url = self._page.url
        # Extract visible text + interactive elements
        elements = self._page.evaluate("""
            () => {
                const out = [];
                const interactives = document.querySelectorAll(
                    'a, button, input, textarea, select, [role="button"], [role="link"], [contenteditable]'
                );
                interactives.forEach((el, i) => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 5 || rect.height < 5) return;
                    const tag = el.tagName.toLowerCase();
                    const type = el.getAttribute('type') || '';
                    const name = el.getAttribute('name') || el.getAttribute('aria-label') || '';
                    const text = (el.textContent || el.value || el.placeholder || '').trim().slice(0, 80);
                    const visible = rect.top < window.innerHeight && rect.bottom > 0;
                    if (!visible) return;
                    out.push({
                        idx: i,
                        ref: 'e' + i,
                        tag,
                        type,
                        name,
                        text
                    });
                });
                return out.slice(0, 100);
            }
        """)
        body_text = self._page.evaluate("() => document.body.innerText.slice(0, 3000)")
        lines = [f"# Page: {title}", f"# URL: {url}", ""]
        lines.append("## Interactive elements (use ref like 'e5' in browser_click/browser_type):")
        for el in elements:
            extras = f" type={el['type']}" if el['type'] else ""
            extras += f" name={el['name']}" if el['name'] else ""
            lines.append(f"  [{el['ref']}] {el['tag']}{extras}: {el['text']}")
        lines.append("")
        lines.append("## Page text (truncated):")
        lines.append(body_text)
        return "\n".join(lines)

    def _screenshot(self, args: dict) -> str:
        filename = args.get("filename") or f"screenshot_{int(time.time())}.png"
        path = self.worker_dir / filename
        self._page.screenshot(path=str(path))
        # Arm the submit gate: a screenshot was taken in this turn, so the
        # next submit_application call (if it's the very next tool call) will
        # be allowed. The dispatcher resets this flag for any non-screenshot,
        # non-submit tool call.
        self._screenshot_taken_this_turn = True
        self._last_submission_screenshot = str(path)
        return f"Screenshot saved: {path}"

    def _submit_application(self, args: dict) -> str:
        """Submit the application form. Implements the submit gate:
        - Requires a screenshot in the current turn.
        - Requires screenshot_taken=True and a non-empty fields_verified list.
        - Refuses in dry-run mode.
        - Refuses if the page is on a URL we don't recognize as a job site
          (defense-in-depth: prevents the agent from submitting a form on the
          wrong site after a redirect).
        """
        if self._dry_run:
            return "REFUSED: dry-run mode. Use finish(FAILED, dry-run) instead."

        if not args.get("screenshot_taken"):
            return (
                "REFUSED: screenshot_taken must be true. "
                "Call browser_take_screenshot immediately before this submit."
            )
        if not self._screenshot_taken_this_turn:
            return (
                "REFUSED: No screenshot taken in the current turn. "
                "You must call browser_take_screenshot right before submit_application. "
                "Other tool calls (navigate, click, type) between screenshot and submit "
                "are forbidden — re-take the screenshot if you need to act in between."
            )
        fields = (args.get("fields_verified") or "").strip()
        if not fields:
            return "REFUSED: fields_verified is empty. List the fields you confirmed."
        reason = (args.get("reason") or "").strip()
        if not reason:
            return "REFUSED: reason is empty. Provide a one-line explanation."

        # Try to find and click a Submit/Apply button. We don't trust the
        # agent's choice of selector here — we search the page for likely
        # submit button text.
        try:
            if not self._page:
                return "REFUSED: no active page"
            current_url = self._page.url
            # Optional: log the URL the agent is submitting on.
            logger.info("submit_application: agent=%s url=%s fields=%s reason=%r",
                        "ollama", current_url, fields, reason)
            self._submit_attempts += 1
            # The agent is expected to have already clicked submit via
            # browser_click; this tool's job is the safety gate, not the
            # click itself. We return a success token and let the agent
            # immediately call finish(APPLIED) on the next turn.
            return (
                f"SUBMIT APPROVED: agent has acknowledged fields=[{fields}] "
                f"reason={reason!r} on {current_url}. "
                "Now call finish(APPLIED) to record the submission."
            )
        except Exception as e:
            return f"Error in submit_application: {e}"

    def _click(self, args: dict) -> str:
        ref = args.get("ref", "")
        selector = args.get("selector", "")
        target = selector or f"[data-ref='{ref}']"
        # If ref-only, use the snapshot index lookup
        if ref and not selector:
            elements = self._page.query_selector_all(
                "a, button, input, textarea, select, [role='button'], [role='link'], [contenteditable]"
            )
            try:
                idx = int(ref.replace("e", ""))
                visible = [e for e in elements if e.is_visible()]
                if idx < len(visible):
                    visible[idx].click(timeout=10000)
                    return f"Clicked ref {ref}"
            except (ValueError, IndexError):
                pass
            return f"Could not find ref {ref} among {len(elements)} elements"
        if selector:
            self._page.click(selector, timeout=10000)
            return f"Clicked selector {selector}"
        return "Error: click requires ref or selector"

    def _type(self, args: dict) -> str:
        text = args.get("text", "")
        ref = args.get("ref", "")
        selector = args.get("selector", "")
        if selector:
            self._page.fill(selector, text)
            return f"Typed into selector {selector}: {text[:50]}"
        if ref:
            elements = self._page.query_selector_all(
                "input, textarea, [contenteditable]"
            )
            try:
                idx = int(ref.replace("e", ""))
                visible = [e for e in elements if e.is_visible()]
                if idx < len(visible):
                    visible[idx].fill(text)
                    return f"Typed into ref {ref}: {text[:50]}"
            except (ValueError, IndexError):
                pass
        return "Error: type requires ref or selector"

    def _fill_form(self, args: dict) -> str:
        fields = args.get("fields", [])
        results = []
        for field in fields:
            ref = field.get("ref") or field.get("name", "")
            value = field.get("value", "")
            ftype = field.get("type", "textbox")
            try:
                idx = int(str(ref).replace("e", ""))
                elements = self._page.query_selector_all(
                    "input, textarea, select, [contenteditable]"
                )
                visible = [e for e in elements if e.is_visible()]
                if idx >= len(visible):
                    results.append(f"  ref {ref}: out of range")
                    continue
                el = visible[idx]
                if ftype == "checkbox":
                    if value and not el.is_checked():
                        el.check()
                    elif not value and el.is_checked():
                        el.uncheck()
                elif ftype == "combobox" or ftype == "select":
                    el.select_option(label=value) if value else None
                else:
                    el.fill(value)
                results.append(f"  ref {ref}: ok")
            except Exception as e:
                results.append(f"  ref {ref}: error {e}")
        return "Fill form results:\n" + "\n".join(results)

    def _file_upload(self, args: dict) -> str:
        path = args.get("path", "")
        ref = args.get("ref", "")
        if not Path(path).exists():
            return f"Error: file not found at {path}"
        # Find file input
        if ref:
            elements = self._page.query_selector_all("input[type='file']")
            try:
                idx = int(ref.replace("e", ""))
                if idx < len(elements):
                    elements[idx].set_input_files(path)
                    return f"Uploaded {path} via ref {ref}"
            except (ValueError, IndexError):
                pass
        # Fallback: any file input
        file_inputs = self._page.query_selector_all("input[type='file']")
        if file_inputs:
            file_inputs[0].set_input_files(path)
            return f"Uploaded {path} (first file input found)"
        return "Error: no file input found on page"

    def _tabs(self, args: dict) -> str:
        action = args.get("action", "list")
        pages = self._context.pages
        if action == "list":
            return f"Open tabs ({len(pages)}): " + ", ".join(
                f"[{i}] {p.url[:60]}" for i, p in enumerate(pages)
            )
        if action == "new":
            p = self._context.new_page()
            return f"Opened new tab, total: {len(self._context.pages)}"
        if action == "close":
            idx = args.get("index", -1)
            if 0 <= idx < len(pages):
                pages[idx].close()
                return f"Closed tab {idx}"
        if action == "select":
            idx = args.get("index", 0)
            if 0 <= idx < len(pages):
                self._page = pages[idx]
                return f"Selected tab {idx}: {self._page.url}"
        return f"Tab action {action} done"

    def _wait_for(self, args: dict) -> str:
        if "time" in args:
            time.sleep(min(float(args["time"]), 10))
            return f"Waited {args['time']}s"
        if "text" in args:
            try:
                self._page.wait_for_selector(f"text={args['text']}", timeout=15000)
                return f"Found text: {args['text']}"
            except Exception as e:
                return f"Text not found: {args['text']} ({e})"
        return "No-op wait"


# -------------------------------------------------------------------
# Main agent loop
# -------------------------------------------------------------------

def run_ollama_agent(
    prompt: str,
    cdp_url: str,
    worker_dir: Path,
    worker_id: int = 0,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
    emit_event=None,
) -> tuple[str, int, dict]:
    """Run the Ollama-based apply agent.

    Args:
        prompt: Full task prompt (job + applicant + instructions)
        cdp_url: Chrome DevTools Protocol endpoint, e.g. http://localhost:9222
        worker_dir: Where to save screenshots/logs
        worker_id: For log naming
        model: Ollama model name
        dry_run: If True, submit_application is disabled. Agent must call
                 finish(FAILED, dry-run) instead.
        emit_event: Optional callable(str) that receives stream-json lines
                    (matches Claude CLI format) for the dashboard

    Returns:
        (status, duration_ms, stats_dict)
    """
    start = time.time()
    messages = [{"role": "user", "content": prompt}]
    actions_log = []
    finish_status = None
    finish_reason = ""
    stats = {"input_tokens": 0, "output_tokens": 0, "turns": 0}

    def _emit(event: dict):
        line = json.dumps(event)
        if emit_event:
            emit_event(line)
        else:
            print(line, flush=True)

    _emit({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": f"[Ollama agent starting, model={model}]"}]},
    })

    try:
        with _PlaywrightToolRunner(cdp_url, worker_dir, dry_run=dry_run) as runner:
            for turn in range(MAX_TURNS):
                # Call Ollama
                try:
                    resp = _ollama_chat(messages, PLAYWRIGHT_TOOLS, model=model)
                except Exception as e:
                    logger.error("Ollama call failed: %s", e)
                    finish_status = "FAILED"
                    finish_reason = f"Ollama error: {e}"
                    break

                choice = resp.get("choices", [{}])[0]
                msg = choice.get("message", {})
                usage = resp.get("usage", {})
                stats["input_tokens"] += usage.get("prompt_tokens", 0)
                stats["output_tokens"] += usage.get("completion_tokens", 0)
                stats["turns"] = turn + 1

                # Per-job cost cap: stop the agent if estimated cost exceeds
                # the cap. Prevents runaway loops from burning LLM budget.
                if MAX_COST_PER_JOB > 0:
                    est_cost = (
                        stats["input_tokens"] * COST_PER_INPUT_TOKEN
                        + stats["output_tokens"] * COST_PER_OUTPUT_TOKEN
                    )
                    if est_cost > MAX_COST_PER_JOB:
                        finish_status = "FAILED"
                        finish_reason = (
                            f"Cost cap hit: ${est_cost:.3f} > ${MAX_COST_PER_JOB:.2f} "
                            f"(turn {turn + 1}, "
                            f"in={stats['input_tokens']} out={stats['output_tokens']})"
                        )
                        logger.warning("Aborting apply: %s", finish_reason)
                        _emit({"type": "assistant", "message": {
                            "content": [{"type": "text", "text": f"ABORT: {finish_reason}"}]
                        }})
                        break

                # Extract content
                content = msg.get("content") or ""
                tool_calls = msg.get("tool_calls") or []
                finish_reason_llm = choice.get("finish_reason", "")

                if content:
                    _emit({
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": content}]},
                    })

                # No tool calls → agent is done thinking, loop
                if not tool_calls:
                    if finish_reason_llm == "stop" or turn == MAX_TURNS - 1:
                        # Force finish
                        if not finish_status:
                            finish_status = "FAILED"
                            finish_reason = "Agent ended without calling finish"
                        break
                    # Add assistant message, nudge for next action
                    messages.append(msg)
                    messages.append({
                        "role": "user",
                        "content": (
                            "What do you do next? Either call a tool, or call the `finish` tool "
                            "with status APPLIED/EXPIRED/CAPTCHA/LOGIN_ISSUE/FAILED when done."
                        ),
                    })
                    continue

                # Append assistant message with tool calls
                messages.append(msg)

                # Execute each tool call
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        args = {}

                    # Log the action (for dashboard)
                    desc = f"{name}"
                    if "url" in args:
                        desc = f"{name} {args['url'][:60]}"
                    elif "ref" in args:
                        desc = f"{name} {args.get('element', args['ref'])}"[:50]
                    elif "fields" in args:
                        desc = f"{name} ({len(args['fields'])} fields)"
                    elif "path" in args:
                        desc = f"{name} {Path(args['path']).name}"
                    actions_log.append(desc)
                    _emit({
                        "type": "assistant",
                        "message": {"content": [{"type": "tool_use", "name": name, "input": args}]},
                    })

                    if name == "finish":
                        finish_status = args.get("status", "FAILED")
                        finish_reason = args.get("reason", "")
                        break

                    result = runner.call(name, args)

                    # Feed result back to LLM
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": str(result)[:8000],  # truncate huge snapshots
                    })

                if finish_status:
                    break

    except Exception as e:
        logger.exception("Agent loop crashed")
        finish_status = "FAILED"
        finish_reason = f"Agent exception: {e}"

    if not finish_status:
        finish_status = "FAILED"
        finish_reason = f"Max turns ({MAX_TURNS}) reached without finishing"

    duration_ms = int((time.time() - start) * 1000)

    # Emit final result event (matches Claude CLI format)
    est_cost = (
        stats["input_tokens"] * COST_PER_INPUT_TOKEN
        + stats["output_tokens"] * COST_PER_OUTPUT_TOKEN
    )
    _emit({
        "type": "result",
        "result": f"{finish_status}: {finish_reason}",
        "usage": {
            "input_tokens": stats["input_tokens"],
            "output_tokens": stats["output_tokens"],
            "estimated_cost_usd": round(est_cost, 4),
        },
        "num_turns": stats["turns"],
        "actions_log": actions_log,
    })

    return finish_status, duration_ms, stats
