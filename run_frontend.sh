#!/usr/bin/env bash
# Launch ApplyPilot Web UI
# Starts the HTML dashboard (default) or Streamlit UI if available

export HOME=/home/bostjan
export PATH="/home/bostjan/.hermes/profiles/osebno/home/.local/bin:$PATH"

# Source Hermes env for LLM keys
source ~/.hermes/.env 2>/dev/null

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Prefer the Streamlit frontend if it exists, otherwise fall back to HTML dashboard
if [ -f "$SCRIPT_DIR/frontend/app.py" ]; then
    echo "Starting Streamlit UI..."
    python3 -m streamlit run "$SCRIPT_DIR/frontend/app.py" \
      --server.port 8501 \
      --server.address localhost \
      --browser.gatherUsageStats=false \
      --theme.base "light"
else
    echo "Starting HTML dashboard..."
    python3 -m applypilot dashboard
fi