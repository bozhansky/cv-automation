#!/usr/bin/env bash
# Launch ApplyPilot Web UI
# Make sure Ollama is running first: ollama serve

export HOME=/home/bostjan

# Run Streamlit
streamlit run "$(dirname "$0")/frontend/app.py" \
  --server.port 8501 \
  --server.address localhost \
  --browser.gatherUsageStats=false \
  --theme.base "light"