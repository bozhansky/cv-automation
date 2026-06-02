"""
MCP Playwright fallback (4.5).

ApplyPilot's primary apply agent is a local Ollama-based Python loop
(applypilot.apply.ollama_agent) that talks to a Playwright browser via CDP.

This module provides a *fallback path* for when that agent is unavailable:
detect if a Playwright MCP server is reachable, and if so, expose the same
browser_* tool surface so the LLM can drive it via MCP's JSON-RPC protocol.

When no MCP server is available, we expose a single helper
`mcp_status()` that returns a user-friendly message + install command.

Usage:
    from applypilot.apply.mcp_fallback import (
        is_mcp_available, mcp_status, get_mcp_tools, MCPServerInfo,
    )

    if is_mcp_available():
        tools = get_mcp_tools()  # list of OpenAI-compatible tool specs
        # ... pass to Ollama with tools=[...]
    else:
        print(mcp_status())  # "MCP not available — install with: ..."

Configuration:
    APPLY_MCP_SERVER_URL  — URL of the MCP server (e.g. http://localhost:8931)
                            Default: APPLY_MCP_AUTO_DISCOVER=1 tries common
                            ports (8931, 3000, 8080) via /sse endpoint probe.
    APPLY_MCP_AUTO_DISCOVER  — "0" to skip auto-discovery
    APPLY_MCP_TIMEOUT  — HTTP timeout in seconds (default 2.0)

The fallback is *opt-in*: by default the Ollama agent runs. To force the
MCP path, set APPLY_USE_MCP=1 and the launcher will route to this module
when the Ollama agent is unavailable.

Installation (when the MCP server isn't available):
    # Option A: install @playwright/mcp globally and run it
    npm install -g @playwright/mcp@latest
    npx @playwright/mcp --port 8931

    # Option B: use a Docker image
    docker run -d -p 8931:8931 mcr.microsoft.com/playwright/mcp

    # Option C: configure a different MCP server
    export APPLY_MCP_SERVER_URL=http://my-mcp-host:8931/sse
"""
from __future__ import annotations

import json
import logging
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Common MCP server ports to probe during auto-discovery
_DEFAULT_MCP_PORTS = (8931, 3000, 8080, 8765, 9000)


@dataclass
class MCPServerInfo:
    """Information about a discovered MCP server."""
    url: str
    version: str | None = None
    tools_count: int | None = None
    healthy: bool = True

    def __str__(self) -> str:
        v = f" v{self.version}" if self.version else ""
        return f"{self.url}{v} ({self.tools_count or '?'} tools)"


def _probe_mcp_endpoint(url: str, timeout: float = 2.0) -> dict | None:
    """Try to fetch MCP server info from `url`. Returns parsed JSON or None."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as e:
        logger.debug("MCP probe failed for %s: %s", url, e)
        return None


def is_mcp_available() -> bool:
    """Check if any Playwright MCP server is reachable. Cheap (~1s)."""
    return discover_mcp_server() is not None


def discover_mcp_server(timeout: float | None = None) -> MCPServerInfo | None:
    """Try to find a Playwright MCP server.

    Search order:
      1. APPLY_MCP_SERVER_URL env var (if set)
      2. Common ports on localhost (8931, 3000, 8080, ...)

    Returns MCPServerInfo on success, None on failure.
    """
    if timeout is None:
        try:
            timeout = float(os.environ.get("APPLY_MCP_TIMEOUT", "2.0"))
        except (TypeError, ValueError):
            timeout = 2.0

    # 1. Explicit URL
    explicit_url = os.environ.get("APPLY_MCP_SERVER_URL", "").strip()
    candidates: list[str] = []
    if explicit_url:
        candidates.append(explicit_url)

    # 2. Auto-discovery (unless disabled)
    if os.environ.get("APPLY_MCP_AUTO_DISCOVER", "1").strip() not in ("0", "false", "no"):
        for port in _DEFAULT_MCP_PORTS:
            candidates.append(f"http://127.0.0.1:{port}/sse")
            candidates.append(f"http://127.0.0.1:{port}/")

    for url in candidates:
        info = _probe_mcp_endpoint(url, timeout=timeout)
        if info is not None:
            return MCPServerInfo(
                url=url,
                version=info.get("version") or info.get("serverInfo", {}).get("version"),
                tools_count=len(info.get("tools", [])) if isinstance(info.get("tools"), list) else None,
                healthy=True,
            )
    return None


def mcp_status() -> str:
    """Return a human-readable status string. Useful for the dashboard."""
    info = discover_mcp_server()
    if info:
        return f"✅ MCP available: {info}"
    return (
        "❌ MCP not available. To enable the fallback, install a Playwright "
        "MCP server:\n"
        "    npm install -g @playwright/mcp@latest\n"
        "    npx @playwright/mcp --port 8931\n"
        "(or set APPLY_MCP_SERVER_URL=http://your-mcp-host:port/sse)"
    )


def get_mcp_tools() -> list[dict]:
    """Return OpenAI-compatible tool specs for the MCP server's playwright tools.

    If the MCP server exposes its tool list at /sse, we fetch and convert.
    Otherwise returns the same set as the local Ollama agent (so a downstream
    LLM can still use them via the MCP transport).
    """
    info = discover_mcp_server()
    if not info:
        return []
    # Fetch the tool list from the MCP server
    try:
        req = urllib.request.Request(info.url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read())
        # MCP returns {"tools": [{name, description, inputSchema}, ...]}
        # Convert to OpenAI-compatible {"type": "function", "function": {...}}
        out = []
        for tool in data.get("tools", []):
            out.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                },
            })
        return out
    except Exception as e:
        logger.warning("Failed to fetch MCP tool list: %s", e)
        return []


def check_local_npx_mcp() -> bool:
    """Check if @playwright/mcp is installed locally via npx (no install required)."""
    try:
        import shutil
        npx = shutil.which("npx")
        if not npx:
            return False
        # npx --no-install fails fast if not cached
        result = os.popen(f"{npx} --no-install @playwright/mcp@latest --version 2>&1").read()
        return "Error" not in result and len(result) < 200
    except Exception:
        return False
