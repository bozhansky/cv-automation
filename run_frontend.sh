#!/usr/bin/env bash
# Launch ApplyPilot Web UI
# Make sure Ollama is running first:  ollama serve
# Then run this script:                ./run_frontend.sh

export HOME=/home/bostjan
export PATH="/home/bostjan/.hermes/profiles/osebno/home/.local/bin:$PATH"

python3 -m streamlit run "$(dirname "$0")/frontend/app.py" \
  --server.port 8501 \
  --server.address localhost \
  --browser.gatherUsageStats=false \
  --theme.base "light"