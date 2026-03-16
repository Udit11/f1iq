#!/usr/bin/env bash
# F1IQ — Start Script
# Run from the f1iq/ directory

set -e
cd "$(dirname "$0")"

echo ""
echo "  ███████╗ ██╗ ██╗ ██████╗ "
echo "  ██╔════╝███║███║██╔═══██╗"
echo "  █████╗  ╚██║╚██║██║   ██║"
echo "  ██╔══╝   ██║ ██║██║▄▄ ██║"
echo "  ██║      ██║ ██║╚██████╔╝"
echo "  ╚═╝      ╚═╝ ╚═╝ ╚══▀▀═╝ "
echo ""
echo "  F1 Intelligence Platform"
echo "  Data: OpenF1 · FastF1 · Ergast"
echo ""

# Install dependencies if missing
if ! python3 -c "import fastapi" 2>/dev/null; then
  echo "Installing dependencies..."
  pip install -r requirements.txt --break-system-packages -q
fi

echo "  Starting server at http://localhost:8000"
echo "  API docs at http://localhost:8000/docs"
echo "  Press Ctrl+C to stop"
echo ""

python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
